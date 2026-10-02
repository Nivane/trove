#!/usr/bin/env python3
"""理解层切片 — 零 LLM/网络/DB 的四个理解维度度量(#1)。

准确率(EX)是一个**终点**数字:它告诉你"答对了几题",不告诉你"卡在
哪一层"。一次通过率(first_pass)进一步区分"首答即对"与"靠恢复兜回来",
但仍然不分层。本脚本把理解链路拆成四个可独立测量的面,全部只读**已提交
的冻结产物**与本地 KB 文件:

  A 语义表达上限    gold SQL 的结构(表/连接/列/过滤值)在语义模型里
                    有没有落点 —— 0% 说明模型缺声明,100% 说明"表达不是
                    瓶颈,损失在下游"。缺口清单 = 重建语义模型的验收表。
  B 分叉归因        MISMATCH 题里,预测与 gold 的首个**结构**分叉组件
                    (tables > projections > conds > groups,余下归
                    ordering_or_other)——"错在哪一类"而不是"错了几题"。
  C 链路归因        schema_linking → query_sketch → compiler → gen
                    哪一环先偏(需要条目带 plan/matched_tables;冻结基线
                    尚未录制 → 打印"未录制",补录后自动生效)。
  D 一次通过率      与分档 EX(compiled/partial/llm),口径直接取自
                    trove/eval/replay.py(单一来源,不另算一份)。

**保守性优先。** 解析失败、限定符无法归属(派生表别名/未知前缀)一律记
"unknown",既不计入覆盖率分母、也绝不进缺口清单 —— 虚报的缺口会让人去
修不存在的问题,比不报更糟。同理,"未声明域"的过滤值(字段没有
enum_display/value_aliases)只计数、不算缺口:模型没声明值域不等于它
表达不了。

退出码(三态,与 check_drift.py 同约定):
  0  报告已产出(缺口/分叉是**产物**,不是失败 —— 它们进报告不进退出码)
  1  度量退化:结果条目为空,或 gold SQL 全部不可解析 —— 报告会输出一个
     空洞的 100%/0 结构,那是"没查成"伪装成"查过了"
  2  没法跑:输入文件缺失、questions 为空、KB 缺 semantics.yml

用法:
  uv run python scripts/eval_understanding.py
  uv run python scripts/eval_understanding.py --json
  uv run python scripts/eval_understanding.py --kb-dir .trove/kb --db-id financial
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from trove.eval.baseline import field_coverage, load_jsonl
from trove.eval.replay import score_replay
from trove.services.lineage.parse import analyze_query
from trove.services.semantic_layer import rls
from trove.services.semantic_layer.compiler import build_contract
from trove.services.semantic_layer.contract import PlanSignature
from trove.services.semantic_layer.models import SemanticDataset, SemanticModel
from trove.services.semantic_layer.provider import SemanticLayerProvider

_DEFAULTS = {
    "questions": Path("eval") / "baseline" / "questions.jsonl",
    "results": Path("eval") / "baseline" / "results.jsonl",
    "kb_dir": Path(".trove") / "kb",
    "db_id": "financial",
    "dialect": "mysql",
}

#: 过滤值检查只认**比较算子**,且列集恰好一列。签名的 conds 是 WHERE 树
#: 逐 Binary 节点抽取的,嵌套 AND/OR 会产生"(多列, 'and', 若干值)"这类
#: 结构噪音 —— 拿它去查值域会造出成倍的假缺口。
_COMPARISON_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "in", "like", "ilike"}

#: SQL 表达式里的标识符扫描:函数名/关键字不是列名,别把它们算成"声明过的列"。
_NON_COLUMN_WORDS = {
    "sum", "count", "avg", "min", "max", "distinct", "cast", "coalesce",
    "abs", "round", "case", "when", "then", "else", "end", "and", "or",
    "not", "null", "if", "nullif", "date", "year", "month", "day", "substr",
    "concat", "as", "asc", "desc", "true", "false", "integer", "decimal",
    "varchar", "char", "float", "double", "text",
}
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: 缺口清单在文本报告里的打印上限(JSON 不截断 —— 它是机器可读产物)。
_TEXT_GAP_LIMIT = 20


# ── 模型侧:声明面 ────────────────────────────────────────────────


def load_semantic_model(kb_dir: Path, db_id: str, dialect: str) -> SemanticModel | None:
    """KB semantics.yml → 语义模型。**零 DB 构造**(同 semantic_query 路由:
    provider 只读文件,catalog/table_exists 都可以不传)。缺文件返回 None,
    由调用方升级为 exit 2 —— 静默空报告会伪装成"模型覆盖为零"。
    """
    semantics = Path(kb_dir) / db_id / "semantics.yml"
    if not semantics.exists():
        return None
    provider = SemanticLayerProvider(
        # directory 是**补充源**且通常不存在(glob 空集即可);不能指向 KB 目录
        # 本身 —— 那里躺着 schema_notes/examples 等无 semantic_model 的 YAML,
        # 解析器会对它们报错,一次失败就把整份模型丢掉(last-known-good=None)。
        directory=Path(kb_dir) / db_id / "semantic",
        datasource=db_id,
        dialect=dialect,
        kb_semantics_path=semantics,
    )
    return provider.model()


def _idents(expression: str) -> set[str]:
    """表达式里的标识符(小写),剔除函数名/类型名/关键字。"""
    return {w.lower() for w in _IDENT_RE.findall(expression or "")} - _NON_COLUMN_WORDS


def declared_surface(model: SemanticModel) -> dict[str, dict[str, Any]]:
    """{物理表: {"columns": set, "domains": {列: set(可锚定的值)}}}。

    columns = 字段名 ∪ 字段表达式里的标识符 ∪ 引用该数据集的度量表达式里的
    标识符。"这个列模型有没有落点"问的就是这个(有落点才可能被表达)。

    domains 只对声明了值域的字段(enum_display / value_aliases)建;键、人类
    标签与别名都算"可锚定的值" —— 三者都是模型能指到同一个存储值的路。
    没声明值域的字段不进 domains(与"声明了但值不在域里"是两回事)。
    """
    surface: dict[str, dict[str, Any]] = {}
    by_name: dict[str, SemanticDataset] = {d.name: d for d in model.datasets}
    for dataset in model.datasets:
        table = rls.physical_table(dataset)
        entry = surface.setdefault(table, {"columns": set(), "domains": {}})
        for field in dataset.fields:
            entry["columns"].add(field.name.lower())
            entry["columns"] |= _idents(field.expression) - {table}
            domain = _value_domain(field.enum_display, field.value_aliases)
            if domain:
                entry["domains"][field.name.lower()] = domain
    for metric in model.metrics:
        idents = _idents(metric.expression)
        for dataset_name in metric.datasets:
            dataset = by_name.get(dataset_name)
            if dataset is None:
                continue
            table = rls.physical_table(dataset)
            entry = surface.setdefault(table, {"columns": set(), "domains": {}})
            entry["columns"] |= idents - {dataset_name.lower()}
    return surface


def _value_domain(
    enum_display: dict[str, str] | None, value_aliases: dict[str, list[str]] | None,
) -> set[str]:
    """字段的可锚定值集合(存储码 ∪ 人类标签 ∪ 别名),小写去空白。"""
    out: set[str] = set()
    for code, label in (enum_display or {}).items():
        out.add(str(code).strip().lower())
        out.add(str(label).strip().lower())
    for code, aliases in (value_aliases or {}).items():
        out.add(str(code).strip().lower())
        out.update(str(a).strip().lower() for a in aliases or [])
    out.discard("")
    return out


def _find_dataset_field(
    model_datasets: list[SemanticDataset], table: str, column: str,
) -> Any | None:
    """(物理表, 列名) → 声明字段(裸列表达式按列名匹配)。找不到返回 None。"""
    for dataset in model_datasets:
        if rls.physical_table(dataset) != table:
            continue
        for field in dataset.fields:
            if field.name.lower() == column:
                return field
            if _idents(field.expression) - {table} == {column}:
                return field
    return None


# ── SQL 侧:局部名与别名解析 ──────────────────────────────────────


def _local_names(sql: str, dialect: str) -> set[str]:
    """SQL 内部的**局部名**:CTE 名与派生表别名。

    这些名字不是物理表 —— 把它们当缺失表名报出去,就是拿"查询内部的名字"
    冒充"模型没声明的表"。解析不了 → 空集(保守:退化为按物理表处理,
    由更外层的 unknown 逻辑兜)。
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return set()
    out = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE) if c.alias_or_name}
    out |= {s.alias.lower() for s in tree.find_all(exp.Subquery) if s.alias}
    return out


def _alias_map(sql: str, dialect: str) -> dict[str, str]:
    """别名 → 物理表(与 lineage.parse._base_aliases 同规则的最小子集)。

    签名里的 join 边与列限定符用的是 SQL 里的写法(``t1``/``T2``),而模型声明
    的是物理表 —— 不解析这一层,每一条 join 边都会被报成未声明。

    同一别名在不同作用域指向不同表(嵌套子查询遮蔽)时**丢弃该别名**:把
    限定符归一错,比不归一会说出更假的话。
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return {}
    out: dict[str, str] = {}
    conflicted: set[str] = set()
    for table in tree.find_all(exp.Table):
        if not table.name:
            continue
        name = _bare_table(table.name)
        key = table.alias.lower() if table.alias else name
        if out.get(key, name) != name:
            conflicted.add(key)
        out[key] = name
    for key in conflicted:
        out.pop(key, None)
    return out


def _bare_table(name: str) -> str:
    """表名归一:去 schema 前缀、小写。"""
    return (name or "").rsplit(".", 1)[-1].strip().lower()


# ── Slice A:语义表达上限 ─────────────────────────────────────────


def check_expressibility(
    qid: str, gold_sql: str, model: SemanticModel, dialect: str,
) -> dict[str, Any]:
    """一题 gold SQL 对语义模型的可表达性(表/连接/列/值四级)。

    返回 ``status="unknown"``(gold 解析不了)或逐级计数与缺口清单。
    """

    def _new() -> dict[str, int]:
        return {"total": 0, "covered": 0, "gaps": 0, "unknown": 0}

    levels: dict[str, dict[str, Any]] = {
        name: _new() for name in ("tables", "joins", "columns", "values")
    }
    gaps: list[dict[str, str]] = []
    unknown: list[str] = []
    undeclared = 0

    def _check(level: str, ok: bool, detail: str) -> None:
        meter = levels[level]
        meter["total"] += 1
        if ok:
            meter["covered"] += 1
        else:
            meter["gaps"] += 1
            gaps.append({"qid": qid, "kind": level, "detail": detail})

    def _skip(level: str, detail: str) -> None:
        levels[level]["unknown"] += 1
        unknown.append(f"{qid} {detail}")

    digest = analyze_query(gold_sql, dialect)
    if digest is None:
        # 形状与可测行**一致**(全零计数):汇总按行取 levels/gaps,少一个键
        # 就会在"gold 有一题解析不了"时把整份报告炸掉 —— 而那时恰恰是最
        # 需要报告的时候。
        return {"qid": qid, "status": "unknown", "reason": "gold SQL 解析不了",
                "levels": levels, "total": 0, "covered": 0,
                "gaps": [], "unknown": [f"{qid} gold SQL 解析不了"],
                "undeclared_values": 0}
    contract = build_contract(gold_sql, dialect)
    signature = contract.signature if contract is not None else None
    surface = declared_surface(model)
    local = _local_names(gold_sql, dialect)
    aliases = _alias_map(gold_sql, dialect)

    # L1 表覆盖:gold 直接引用的表名(CTE/派生表别名除外出)
    for table in digest.tables_read:
        name = _bare_table(table)
        if name in local:
            continue
        _check("tables", name in surface, f"缺表 {name}")

    # L1.5 连接覆盖:每条 join 边 → 两边表是否被声明的关系连起来
    edges = contract.join_edges if contract is not None else ()
    declared_edges = {
        frozenset({_bare_table(r.from_), _bare_table(r.to)}) for r in model.relationships
    }
    if signature is None:
        _skip("joins", "无签名,连接未检查")
    else:
        for left, right in edges:
            ta = aliases.get(left[0], _bare_table(left[0]))
            tb = aliases.get(right[0], _bare_table(right[0]))
            if not ta or not tb or ta in local or tb in local:
                _skip("joins", f"连接 {left[0] or '?'}→{right[0] or '?'} 限定符无法归属")
                continue
            _check("joins", frozenset({ta, tb}) in declared_edges, f"缺连接 {ta}↔{tb}")

    # L2 列覆盖:列名在所属表的声明面里(空限定符 → 全模型查;查不到记 unknown)
    all_columns = set().union(*(e["columns"] for e in surface.values())) if surface else set()
    for table, column in digest.columns_read:
        name = _bare_table(table)
        col = column.lower()
        if name in local:
            # CTE/派生表限定:那是查询内部的名字,归不到物理表 —— 记 unknown
            # 而不是**静默丢掉**(悄悄少算一项,分母就与"模型覆盖多少"无关了)
            _skip("columns", f"列 {name}.{column} 限定符是 CTE/派生表名,列覆盖无从判定")
        elif name:
            if name not in surface:
                _skip("columns", f"列 {name}.{column} 的表未声明,列覆盖无从判定")
                continue
            _check("columns", col in surface[name]["columns"], f"缺列 {name}.{column}")
        elif col in all_columns:
            _check("columns", True, "")
        else:
            _skip("columns", f"列 {column} 限定符无法归属")

    # L3 值覆盖:过滤值 ∈ 字段声明值域。域未声明 → 只计数;列归属不明 → unknown
    conds = signature.conds if signature is not None else ()
    for cols, op, values in conds:
        if op not in _COMPARISON_OPS or len(cols) != 1 or not values:
            continue
        _, column = cols[0]
        owners = {
            _bare_table(t) for t, c in digest.columns_read
            if c.lower() == str(column).lower() and _bare_table(t) and _bare_table(t) not in local
        }
        if len(owners) != 1:
            _skip("values", f"过滤列 {column} 归属不明({len(owners)} 个候选表)")
            continue
        table = next(iter(owners))
        field = _find_dataset_field(model.datasets, table, str(column).lower())
        domain = _value_domain(
            getattr(field, "enum_display", None), getattr(field, "value_aliases", None),
        ) if field is not None else set()
        if not domain:
            undeclared += len(values)
            continue
        for value in values:
            _check(
                "values", str(value).strip().lower() in domain,
                f"缺值 {table}.{column}={value}",
            )

    totals = {k: v["total"] for k, v in levels.items()}
    covered = {k: v["covered"] for k, v in levels.items()}
    return {
        "qid": qid,
        "status": "ok",
        "levels": levels,
        "total": sum(totals.values()),
        "covered": sum(covered.values()),
        "gaps": gaps,
        "unknown": unknown,
        "undeclared_values": undeclared,
    }


def expressibility_slice(
    questions: list[dict[str, Any]], model: SemanticModel, dialect: str,
) -> dict[str, Any]:
    """Slice A 汇总:逐题结果 + 分档计数 + 缺口清单。"""
    per_question = [
        check_expressibility(str(q.get("qid") or q.get("question") or "?"),
                             str(q.get("gold_sql") or ""), model, dialect)
        for q in questions
    ]
    ok_rows = [r for r in per_question if r["status"] == "ok"]
    levels: dict[str, dict[str, int]] = {}
    for name in ("tables", "joins", "columns", "values"):
        levels[name] = {
            "total": sum(r["levels"][name]["total"] for r in ok_rows),
            "covered": sum(r["levels"][name]["covered"] for r in ok_rows),
            "gaps": sum(r["levels"][name]["gaps"] for r in ok_rows),
            "unknown": sum(r["levels"][name]["unknown"] for r in ok_rows),
        }
    total = sum(v["total"] for v in levels.values())
    covered = sum(v["covered"] for v in levels.values())
    return {
        "n_questions": len(per_question),
        "n_measurable": len(ok_rows),
        "n_unknown": len(per_question) - len(ok_rows),
        "levels": levels,
        "total": total,
        "covered": covered,
        "coverage": round(covered / total, 4) if total else 0.0,
        "gaps": [g for r in per_question for g in r["gaps"]],
        "unknown": [u for r in per_question for u in r["unknown"]],
        "undeclared_values": sum(r["undeclared_values"] for r in per_question),
        "per_question": per_question,
    }


# ── Slice B:分叉归因 ─────────────────────────────────────────────

#: 首个分叉组件的固定优先级。tables 用谱系摘要(实际读到的物理表)而不是
#: 签名里的表名:签名只取每个 FROM/JOIN 源的**第一个** Table 节点 —— CTE 包装
#: 的查询会被记成 CTE 名、而 CTE 体内真实读的表反而不计(实测 0472:pred 被
#: 记成"丢 client、多 selected_client",物理上两张表都读了)。
#: 其余组件(projections/conds/groups/joins)沿用 PlanSignature 的比较规则。
_DIVERGENCE_PRIORITY = ("tables", "projections", "conds", "groups")


def _base_tables(digest: Any, sql: str, dialect: str) -> set[str]:
    """实际读取的物理表(谱系摘要,减去 CTE 名/派生表别名)。"""
    local = _local_names(sql, dialect)
    return {
        _bare_table(str(t)) for t in (digest.tables_read or [])
        if str(t).strip() and _bare_table(str(t)) not in local
    }


def _normalize_cols(
    cols: tuple[Any, ...], aliases: dict[str, str], local: set[str],
) -> tuple[tuple[str, str], ...]:
    """列集限定符归一:别名 → 物理表;CTE/派生表限定 → 去限定(只比列名)。

    归一是**单向收窄**:它只会让"写法不同、物理同源"的一对列判为相同
    (别名 ``a`` vs 物理名 ``account``;CTE 名 ``sc`` vs 其体内的表),
    不会把真正不同的列洗成相同 —— 除非把 CTE 体内的表也解析错,而那种
    情况下 CTE 名本来也无法归属,按列名比是这里能给出的最保守答案。
    """
    out: list[tuple[str, str]] = []
    for col in cols:
        table, column = str(col[0] or "").lower(), str(col[1] or "").lower()
        resolved = aliases.get(table, table)
        if table and (table in local or resolved in local):
            resolved = ""  # CTE/派生表限定 → 无法归到物理表,只比列名
        out.append((resolved, column))
    return tuple(sorted(out))


def _first_divergence(
    gold_sql: str, pred_sql: str, gsig: PlanSignature, psig: PlanSignature,
    gold_tables: set[str], pred_tables: set[str], dialect: str,
) -> tuple[str, str]:
    """首个分叉组件 + 明细;找不到结构分叉 → (``"none"``, 说明)。

    列级比较**逐条复用** PlanSignature._cols_match(裸列 vs 限定列宽容),
    只是先过一遍限定符归一(别名/CTE 写法不该被报成结构分叉)。
    """
    if gold_tables != pred_tables:
        return "tables", (
            f"读表 {'+'.join(sorted(gold_tables)) or '—'}"
            f" → {'+'.join(sorted(pred_tables)) or '—'}"
        )
    galias, palias = _alias_map(gold_sql, dialect), _alias_map(pred_sql, dialect)
    glocal, plocal = _local_names(gold_sql, dialect), _local_names(pred_sql, dialect)
    gcols = lambda cs: _normalize_cols(cs, galias, glocal)  # noqa: E731
    pcols = lambda cs: _normalize_cols(cs, palias, plocal)  # noqa: E731

    if len(gsig.projections) != len(psig.projections):
        return "projections", f"投影数 {len(gsig.projections)} → {len(psig.projections)}"
    for (gf, gcs), (pf, pcs) in zip(gsig.projections, psig.projections):
        if gf != pf or not PlanSignature._cols_match(gcols(gcs), pcols(pcs)):
            return "projections", (
                f"投影 {gf or '列'}({_cols_text(gcols(gcs))})"
                f" → {pf or '列'}({_cols_text(pcols(pcs))})"
            )
    if len(gsig.conds) != len(psig.conds):
        return "conds", f"过滤数 {len(gsig.conds)} → {len(psig.conds)}"
    for (gcs, gop, gvals), (pcs, pop, pvals) in zip(gsig.conds, psig.conds):
        if gop != pop or gvals != pvals or not PlanSignature._cols_match(gcols(gcs), pcols(pcs)):
            return "conds", (
                f"过滤 {_cols_text(gcols(gcs))} {gop} {list(gvals)}"
                f" → {_cols_text(pcols(pcs))} {pop} {list(pvals)}"
            )
    if gsig.groups != psig.groups:
        return "groups", f"分组宽度 {gsig.groups} → {psig.groups}"
    if gsig.joins != psig.joins:
        return "ordering_or_other", f"连接数 {gsig.joins} → {psig.joins}"
    # 全部组件在归一后一致 → 差异只剩写法(表名/限定符拼法)。**不**报成结构
    # 分叉:错的是取值/排序/表达式语义,那是签名刻意不捕获的维度。
    if gsig.tables != psig.tables:
        return "none", "物理表集相同,签名表名差异仅 CTE/派生表命名"
    return "none", "结构等价(别名/限定符归一后),分叉在结构之外"


def _cols_text(cols: tuple[tuple[str, str], ...]) -> str:
    return ",".join(f"{t}.{c}" if t else c for t, c in cols) or "*"


def attribute_divergence(
    qid: str, pred_sql: str, gold_sql: str, dialect: str,
) -> dict[str, Any]:
    """一题 MISMATCH 的首个结构分叉组件(bucket + 明细)。"""
    gold = build_contract(gold_sql, dialect)
    pred = build_contract(pred_sql, dialect)
    gsig = gold.signature if gold is not None else None
    psig = pred.signature if pred is not None else None
    if gsig is None or psig is None:
        return {"qid": qid, "bucket": "unknown", "detail": "一方 SQL 无签名"}
    if gsig.matches(psig):
        # 形状等价但结果不同:分叉在签名捕获不到的维度(排序/取值语义等),
        # 报"无结构分叉"而不是硬塞进某个组件。
        return {"qid": qid, "bucket": "none", "detail": "签名等价,分叉在结构之外"}
    gdig, pdig = analyze_query(gold_sql, dialect), analyze_query(pred_sql, dialect)
    if gdig is None or pdig is None:
        return {"qid": qid, "bucket": "unknown", "detail": "一方 SQL 谱系摘要不可用"}
    component, detail = _first_divergence(
        gold_sql, pred_sql, gsig, psig,
        _base_tables(gdig, gold_sql, dialect), _base_tables(pdig, pred_sql, dialect),
        dialect,
    )
    return {"qid": qid, "bucket": component, "detail": detail}


def divergence_slice(entries: list[dict[str, Any]], dialect: str) -> dict[str, Any]:
    """Slice B 汇总:MISMATCH 且两侧都有 SQL 的条目,按首个分叉组件计数。"""
    rows = [
        e for e in entries
        if e.get("verdict") == "MISMATCH"
        and str(e.get("pred_sql") or "").strip()
        and str(e.get("gold_sql") or "").strip()
    ]
    results = [
        attribute_divergence(
            str(e.get("qid") or e.get("question") or "?"),
            str(e["pred_sql"]), str(e["gold_sql"]), dialect,
        )
        for e in rows
    ]
    buckets: dict[str, int] = {}
    for r in results:
        buckets[r["bucket"]] = buckets.get(r["bucket"], 0) + 1
    n_mismatch = sum(1 for e in entries if e.get("verdict") == "MISMATCH")
    return {
        "n_mismatch": n_mismatch,
        "n_attributable": len(results),
        "buckets": buckets,
        "entries": results,
    }


# ── Slice C:链路归因 ─────────────────────────────────────────────


def _plan_tables(plan: dict[str, Any]) -> set[str]:
    return {_bare_table(str(t)) for t in (plan.get("tables") or []) if str(t).strip()}


def _gold_filter_values(signature: PlanSignature | None) -> int:
    if signature is None:
        return 0
    return sum(
        1 for cols, op, values in signature.conds
        if op in _COMPARISON_OPS and len(cols) == 1 and values
    )


def attribute_chain(entry: dict[str, Any], dialect: str) -> dict[str, Any]:
    """一条结果条目的首个偏离环节(schema_linking → query_sketch →
    compiler → gen)。只对**未命中**的题找第一个偏的环:命中题全链都对,
    归因没有信息量。

    ``status="unrecorded"`` = 条目没带 plan/matched_tables(冻结基线如此),
    此时归因**未知**,不是"没有偏离"。
    """
    plan = entry.get("plan")
    matched = entry.get("matched_tables")
    if not plan and not matched:
        return {"qid": entry.get("qid") or "?", "bucket": "unrecorded", "detail": ""}
    if entry.get("verdict") == "MATCH":
        return {"qid": entry.get("qid") or "?", "bucket": "ok", "detail": ""}
    gold_sql = str(entry.get("gold_sql") or "")
    gold = build_contract(gold_sql, dialect) if gold_sql else None
    gsig = gold.signature if gold is not None else None
    gold_tables = set(gsig.tables) if gsig is not None else set()

    # ① schema_linking:命中表是否覆盖 gold 表
    if matched:
        hit = {_bare_table(str(t)) for t in matched if str(t).strip()}
        missing = sorted(gold_tables - hit)
        if missing:
            return {"qid": entry.get("qid") or "?",
                    "bucket": "schema_linking", "detail": f"漏表 {', '.join(missing)}"}
    # ② query_sketch:计划表是否覆盖 gold 表;gold 有过滤而计划无条件
    if isinstance(plan, dict) and plan:
        planned = _plan_tables(plan)
        missing = sorted(gold_tables - planned) if gold_tables else []
        if missing:
            return {"qid": entry.get("qid") or "?",
                    "bucket": "query_sketch", "detail": f"计划漏表 {', '.join(missing)}"}
        if _gold_filter_values(gsig) and not (plan.get("conditions") or []):
            return {"qid": entry.get("qid") or "?",
                    "bucket": "query_sketch", "detail": "gold 有过滤,计划无条件"}
    # ③ compiler:有 compile_meta 且没吃到 compiled
    meta = entry.get("compile_meta")
    if isinstance(meta, dict) and meta and meta.get("outcome") != "compiled":
        detail = str(meta.get("miss_component") or "") or str(meta.get("miss_reason") or "")
        return {"qid": entry.get("qid") or "?",
                "bucket": "compiler", "detail": f"{meta.get('outcome')}: {detail}".strip(": ")}
    # ④ gen:生成 SQL 与 gold 的签名比对
    pred_sql = str(entry.get("pred_sql") or "")
    if not pred_sql:
        return {"qid": entry.get("qid") or "?",
                "bucket": "gen", "detail": "无生成 SQL"}
    if gsig is None:
        return {"qid": entry.get("qid") or "?",
                "bucket": "unknown", "detail": "gold 无签名"}
    p = build_contract(pred_sql, dialect)
    psig = p.signature if p is not None else None
    if psig is None:
        return {"qid": entry.get("qid") or "?",
                "bucket": "gen", "detail": "生成 SQL 解析不了"}
    if psig.matches(gsig):
        return {"qid": entry.get("qid") or "?",
                "bucket": "none", "detail": "生成结构等价,分叉在结构之外"}
    return {"qid": entry.get("qid") or "?", "bucket": "gen", "detail": "生成结构偏离计划"}


def chain_slice(entries: list[dict[str, Any]], dialect: str) -> dict[str, Any]:
    """Slice C 汇总:录制覆盖 + 逐条目归因。"""
    recorded = [e for e in entries if e.get("plan") or e.get("matched_tables")]
    # 逐条一行(**含未录制**):某题"归不了因"与"归到 ok"是两回事,前者必须
    # 在条目里看得见,否则补录不完整的报告会显得比实际干净。
    results = [attribute_chain(e, dialect) for e in entries]
    buckets: dict[str, int] = {}
    for r in results:
        if r["bucket"] == "unrecorded":
            continue
        buckets[r["bucket"]] = buckets.get(r["bucket"], 0) + 1
    if not recorded:
        status = "unrecorded"
    elif len(recorded) < len(entries):
        status = "partial"
    else:
        status = "ok"
    return {
        "n_entries": len(entries),
        "n_recorded": len(recorded),
        "status": status,
        "buckets": buckets,
        "entries": results,
    }


# ── Slice D:一次通过率 + 分档 EX ─────────────────────────────────


def first_pass_slice(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Slice D:口径直接取自 score_replay(单一来源),另附 path 覆盖度。

    分档 EX 只在 path 覆盖完整时才由 score_replay 发出;不完整时这里显式
    报告"未发"与覆盖分子分母 —— "没发键"与"发了但为空"必须可区分。
    """
    score = score_replay(entries)
    path_cov = field_coverage(entries).get("path", 0.0)
    return {
        "first_pass": score.get("first_pass"),
        "ex": score.get("ex"),
        "ex_judged": score.get("ex_judged"),
        "n": score.get("n"),
        "ex_by_path": score.get("ex_by_path"),
        "path_coverage": path_cov,
    }


# ── 渲染 ─────────────────────────────────────────────────────────

_LEVEL_LABELS = {"tables": "缺表", "joins": "缺连接", "columns": "缺列", "values": "缺值"}


def _pct(n: int, d: int) -> str:
    return f"{n / d:.1%}" if d else "—"


def render_report(report: dict[str, Any]) -> str:
    """人读报告(与 --json 同一份数据的文本投影)。"""
    lines: list[str] = [f"理解层切片 · {report['db_id']} ({report['dialect']})"]

    a = report["expressibility"]
    lines.append(
        f"语义表达上限: {a['n_measurable']}/{a['n_questions']} 题可测 · "
        f"结构检查 {a['total']} 项覆盖 {a['covered']} ({_pct(a['covered'], a['total'])})"
    )
    parts = [
        f"{_LEVEL_LABELS[k]} {v['gaps']}/{v['total']}"
        for k, v in a["levels"].items()
    ]
    lines.append(
        "  逐级: " + " · ".join(parts)
        + f" · 未声明值 {a['undeclared_values']} · unknown {len(a['unknown'])}"
    )
    for gap in a["gaps"][:_TEXT_GAP_LIMIT]:
        lines.append(f"    ✗ {gap['qid']} {gap['detail']}")
    if len(a["gaps"]) > _TEXT_GAP_LIMIT:
        lines.append(f"    …(另 {len(a['gaps']) - _TEXT_GAP_LIMIT} 条,见 --json)")

    b = report["divergence"]
    b_parts = " · ".join(f"{k} {v}" for k, v in sorted(b["buckets"].items()))
    lines.append(
        f"分叉归因: {b['n_attributable']}/{b['n_mismatch']} 题 MISMATCH 可归因"
        + (f" → {b_parts}" if b_parts else "")
    )
    for r in b["entries"]:
        if r["bucket"] not in ("none", "unknown"):
            lines.append(f"    · {r['qid']} [{r['bucket']}] {r['detail']}")

    c = report["links"]
    if c["status"] == "unrecorded":
        lines.append(
            f"链路归因: 未录制 —— {c['n_entries']}/{c['n_entries']} 题缺 "
            "plan/matched_tables(补录后自动生效)"
        )
    else:
        c_parts = " · ".join(f"{k} {v}" for k, v in sorted(c["buckets"].items()))
        lines.append(
            f"链路归因: {c['n_recorded']}/{c['n_entries']} 题已录制 [{c['status']}]"
            + (f" → {c_parts}" if c_parts else "")
        )
        for r in c["entries"]:
            if r["bucket"] not in ("ok", "none", "unrecorded"):
                lines.append(f"    · {r['qid']} [{r['bucket']}] {r['detail']}")

    d = report["first_pass"]
    fp = d["first_pass"]
    lines.append(
        "一次通过率: "
        + (f"{fp:.4f} ({round(fp * (d['ex_judged'] or 0))}/{d['ex_judged']})" if fp is not None else "—")
        + f" · EX {d['ex']:.4f} ({d['ex_judged']} 可判)"
    )
    if d["ex_by_path"]:
        lines.append(
            "  分档 EX: " + " · ".join(f"{k} {v:.4f}" for k, v in d["ex_by_path"].items())
        )
    else:
        lines.append(
            f"  分档 EX 未发:path 覆盖 {_pct(round(d['path_coverage'] * d['n']), d['n'])}"
            f"({round(d['path_coverage'] * d['n'])}/{d['n']}),不完整(半份 path 的分档是把缺数据当 llm 档)"
        )
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="理解层切片(零 LLM/网络/DB)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--questions", default=str(_DEFAULTS["questions"]))
    p.add_argument("--results", default=str(_DEFAULTS["results"]))
    p.add_argument("--kb-dir", default=str(_DEFAULTS["kb_dir"]))
    p.add_argument("--db-id", default=_DEFAULTS["db_id"])
    p.add_argument("--dialect", default=_DEFAULTS["dialect"])
    p.add_argument("--json", action="store_true", help="只输出 JSON")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    questions_path = Path(args.questions)
    results_path = Path(args.results)
    if not questions_path.exists():
        print(f"× 问题集不存在: {questions_path}", file=sys.stderr)
        return 2
    if not results_path.exists():
        print(f"× 结果文件不存在: {results_path}", file=sys.stderr)
        return 2
    questions = load_jsonl(questions_path)
    if not questions:
        print(f"× 问题集为空: {questions_path}", file=sys.stderr)
        return 2
    model = load_semantic_model(Path(args.kb_dir), args.db_id, args.dialect)
    if model is None:
        print(
            f"× 缺语义模型: {Path(args.kb_dir) / args.db_id / 'semantics.yml'}"
            "(先 /kb init 或指定 --kb-dir/--db-id)",
            file=sys.stderr,
        )
        return 2
    entries = load_jsonl(results_path)

    report: dict[str, Any] = {
        "db_id": args.db_id,
        "dialect": args.dialect,
        "questions_path": str(questions_path),
        "results_path": str(results_path),
        "expressibility": expressibility_slice(questions, model, args.dialect),
        "divergence": divergence_slice(entries, args.dialect),
        "links": chain_slice(entries, args.dialect),
        "first_pass": first_pass_slice(entries),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_report(report))

    # 度量退化(不是"有缺口"):空条目或全不可解析会产出空洞的 100%/0,
    # 那是"没查成"伪装成"查过了" —— 与 check_drift.py 的 2 号语义同源。
    if not entries or report["expressibility"]["total"] == 0:
        print("× 度量退化:条目为空或 gold SQL 全部不可解析", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
