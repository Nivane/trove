#!/usr/bin/env python3
"""重生成 KB 里「生成器拥有」的两段产物(确定性,零 LLM)。

`kb init` 的确定性生成器从 `schema_notes.yml` 的列描述直接产出两段内容:

- **metrics**(`semantics.yml`):`generate_terms` → `terms_to_ossie_document`;
- **few-shot 模板**(`examples.yml`):`generate_templates`。

生成器修复(噪音子句剥离、率值守卫等)后,存量 KB 的旧产物需按新规则重建。

**metrics**:只重建生成器拥有的条目——表达式是单表简单聚合
(``COUNT/SUM/AVG(表.列)`` 或 ``AVG(EXTRACT(YEAR FROM 表.列))``),且表是本
文件声明过的 dataset、列也在该 dataset 的 fields 里。MAX/MIN 等生成器不
产出的形状不在其列——人工 metric(`max_loan_amount`)不会被误删。

**examples**:`template: True` 的条目里混着两类东西——确定性模板与
LLM 合成 few-shot(`synthetic.validate_examples` 给两者打同一个标志,
文件里无法区分)。**tags 是唯一可用的判别式**:生成器的签名 tags 是
``[表, 种类, aggregation]``(如 ``[card, group, aggregation]``),合成的
是描述性短标签(如 ``[card, aggregation]``)。因此**不做整体重建**,只做
两件可证明的事:
1. **问句对齐**:SQL 命中当前生成器的模板 **且 tags 与模板逐字相同**的
   条目,问句替换为生成器现在的措辞(噪音子句/尾标点被剥掉的那个版本)。
   只看 SQL 不够——合成条目可以复用模板 SQL 却带更自然的人工问句
   (demo 的 "How many cards are there of each type?" 即此形),对齐会把
   它换成机械问句,是退步;
2. **陈旧条目剔除**:SQL 是单表聚合形状(``SELECT FUNC(列) FROM 表``)、
   tags 是生成器签名、且当前生成器**不再产出**的条目(率值列的 SUM
   模板——对率求和是错例,而 few_shots 是优先级最高的上下文块)。
   其余条目(LLM 合成的多表/HAVING/日期区间等)一字不动。

**范围警告**:``mysql_fin`` 里有一条人工夹具
(``loan_count_in_2020``)与生成器产物**形状完全一致**(``COUNT(表.列)``
+ 正确 dataset),脚本按定义无法与生成器产物区分——对它 ``--write`` 会把
夹具静默改名(实测 dry-run:``loan_count_in_2020`` → ``number of loan
records``)。**不要对 mysql_fin 跑 --write**,除非先接受/处理该改名。

防呆:
- 待重建 metric 若带生成器不会写的键(name/expression/description/
  ai_context 之外,或 ai_context 里 synonyms 之外的键)→ 报错退出;
- 陈旧条目剔除前逐条打印,落盘前不给"静默删除";
- 重写前先验 YAML 往返 byte-identical,不保真则拒绝落盘。

用法:
    uv run python scripts/regen_kb_generated.py                 # dry-run 看 diff
    uv run python scripts/regen_kb_generated.py --write         # 落盘
    uv run python scripts/regen_kb_generated.py --check         # 有漂移则退出码 1
    uv run python scripts/regen_kb_generated.py --only metrics  # 只跑一段
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from trove.services.kb.deterministic_gen import generate_terms, generate_templates
from trove.services.kb.ossie_format import terms_to_ossie_document

_DUMP_KWARGS = dict(default_flow_style=False, allow_unicode=True, sort_keys=False)

# 生成器自己会写的键;其余键 = 人工加工,遇到即报错(不静默丢)
_GENERATED_KEYS = ("name", "expression", "description", "ai_context")
_AI_CONTEXT_KEYS = ("synonyms",)

_SIMPLE_AGG_RE = re.compile(
    r"^(?:COUNT|SUM|AVG)\(\s*(?P<table>\"?[A-Za-z_]\w*\"?)\.(?P<column>\w+)\s*\)$"
)
_YEAR_AGG_RE = re.compile(
    r"^AVG\(EXTRACT\(YEAR FROM\s+(?P<table>\"?[A-Za-z_]\w*\"?)\.(?P<column>\w+)\)\)$"
)
# 条件占比度量(生成器产物,见 deterministic_gen._share_terms):
#   CAST(SUM(CASE WHEN <表>.<枚举列> = '<值>' THEN 1 ELSE 0 END) AS DOUBLE)
#       * 100.0 / COUNT(*)
#   CAST(SUM(CASE WHEN <表>.<枚举列> = '<值>' THEN <表>.<度量列> ELSE 0 END)
#       AS DOUBLE) * 100.0 / NULLIF(SUM(<表>.<度量列>), 0)
# 捕获 (表, 枚举列[, 度量列]);引用的列都须在声明的 dataset fields 内才认。
# 分子聚外的 CAST 先由 _unwrap_double_cast 剥掉 —— 形态换代期旧盘上还留着
# 无 CAST 形态,两者都属生成器所有,不能把旧的误判成人工条目(报错/冻结)。
_SHARE_COUNT_RE = re.compile(
    r"^SUM\(CASE WHEN (?P<table>\"?[A-Za-z_]\w*\"?)\.(?P<column>\w+) = '(?:[^']|'')+'"
    r" THEN 1 ELSE 0 END\) \* 100\.0 / COUNT\(\*\)$"
)
_SHARE_MEASURE_RE = re.compile(
    r"^SUM\(CASE WHEN (?P<table>\"?[A-Za-z_]\w*\"?)\.(?P<column>\w+) = '(?:[^']|'')+'"
    r" THEN (?P<table2>\"?[A-Za-z_]\w*\"?)\.(?P<measure>\w+) ELSE 0 END\) \* 100\.0"
    r" / NULLIF\(SUM\((?P=table2)\.(?P=measure)\), 0\)$"
)

#: 表达式起始处的 ``CAST(``(生成器占比形态:分子包一层)。
_DOUBLE_CAST_HEAD_RE = re.compile(r"^CAST\s*\(", re.I)
#: CAST 内容必须以 ``AS DOUBLE`` 收尾(贪婪 ``.*`` = 取最靠右的那对)。
_DOUBLE_CAST_TAIL_RE = re.compile(r"^(?P<inner>.*)\s+AS\s+DOUBLE\s*$", re.I | re.S)


def _matching_paren(text: str, open_idx: int) -> int | None:
    """``text[open_idx] == '('`` 的配对右括号下标;不配对 → None。

    单引号串内的括号不计(``''`` 是转义的字面量单引号)。
    """
    depth = 0
    in_quote = False
    i = open_idx
    while i < len(text):
        ch = text[i]
        if in_quote:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                in_quote = False
        elif ch == "'":
            in_quote = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _unwrap_double_cast(expr: str) -> str:
    """剥掉最外层 ``CAST(<x> AS DOUBLE)`` 的外壳,返回其余部分原样。

    形态识别专用(``_is_generator_owned`` 先归一再看形状),不动落盘内容
    —— diff 仍按原文比较。生成器占比的 CAST 只包**分子**:表达式
    ``CAST(SUM(...) AS DOUBLE) * 100.0 / COUNT(*)`` 剥成
    ``SUM(...) * 100.0 / COUNT(*)``,正好落回既有的占比正则。

    只认**表达式起始处**的 CAST(生成器只产这一种摆法),且要求括号配对、
    内容以 ``AS DOUBLE`` 收尾、内层非空 —— 任一条不满足即原样返回:多剥
    一层就可能把人工表达式误认成生成器产物(被静默重建/删除)。
    """
    text = str(expr or "").strip()
    head = _DOUBLE_CAST_HEAD_RE.match(text)
    if not head:
        return text
    close = _matching_paren(text, head.end() - 1)
    if close is None:
        return text
    tail = _DOUBLE_CAST_TAIL_RE.match(text[head.end():close])
    if not tail:
        return text
    inner = tail.group("inner").strip()
    if not inner:
        return text
    return inner + text[close + 1:]


# D 族模板的 SQL 形状(单表聚合,tags = [表, 列, aggregation])
_TEMPLATE_AGG_RE = re.compile(
    r"^SELECT (?:SUM|AVG|MAX|MIN)\(\w+\) FROM \"?[A-Za-z_]\w*\"?$"
)


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _expr_of(metric: dict[str, Any]) -> str:
    dialects = (metric.get("expression") or {}).get("dialects") or []
    return str(dialects[0].get("expression", "") or "") if dialects else ""


def _is_generator_owned(expr: str, datasets: dict[str, set[str]]) -> bool:
    # 先剥最外层 CAST(x AS DOUBLE):新旧两种占比形态都算生成器拥有。
    text = _unwrap_double_cast(expr)
    for pattern in (_SIMPLE_AGG_RE, _YEAR_AGG_RE):
        m = pattern.match(text)
        if not m:
            continue
        table = m.group("table").strip('"')
        return table in datasets and m.group("column") in datasets[table]
    m = _SHARE_COUNT_RE.match(text)
    if m:
        table = m.group("table").strip('"')
        return table in datasets and m.group("column") in datasets[table]
    m = _SHARE_MEASURE_RE.match(text)
    if m:
        table = m.group("table").strip('"')
        measure_table = m.group("table2").strip('"')
        return (
            table in datasets
            and m.group("column") in datasets[table]
            and measure_table in datasets
            and m.group("measure") in datasets[measure_table]
        )
    return False


def _foreign_keys(metric: dict[str, Any]) -> list[str]:
    """生成器不会写的键(人工加工痕迹)。"""
    extra = [k for k in metric if k not in _GENERATED_KEYS]
    ai = metric.get("ai_context")
    if isinstance(ai, dict):
        extra += [f"ai_context.{k}" for k in ai if k not in _AI_CONTEXT_KEYS]
    elif ai is not None:
        extra.append("ai_context")
    return extra


# ── metrics ──────────────────────────────────────────────


def _rebuild_metrics(old_metrics: list[dict[str, Any]], new_metrics: list[dict[str, Any]],
                     datasets: dict[str, set[str]]) -> tuple[list[dict[str, Any]], list[str]]:
    """旧 metrics 列表 → 生成器拥有的条目替换为 new_metrics(原位置),返回错误列表。"""
    errors: list[str] = []
    owned: list[dict[str, Any]] = []
    for mt in old_metrics:
        if _is_generator_owned(_expr_of(mt), datasets):
            owned.append(mt)
            foreign = _foreign_keys(mt)
            if foreign:
                errors.append(f"{mt.get('name')!r} 带生成器不会写的键 {foreign}")
    if errors:
        return old_metrics, errors

    first = next(
        (i for i, mt in enumerate(old_metrics) if _is_generator_owned(_expr_of(mt), datasets)),
        len(old_metrics),
    )
    kept_tail = [
        mt for mt in old_metrics[first:] if not _is_generator_owned(_expr_of(mt), datasets)
    ]
    return old_metrics[:first] + new_metrics + kept_tail, []


def _render_metric_diff(owned: list[dict[str, Any]], new_metrics: list[dict[str, Any]]) -> str:
    old_by_expr = {_expr_of(mt): mt for mt in owned}
    new_by_expr = {_expr_of(mt): mt for mt in new_metrics}
    removed = [e for e in old_by_expr if e not in new_by_expr]
    added = [e for e in new_by_expr if e not in old_by_expr]
    renamed = [
        e for e in new_by_expr
        if e in old_by_expr and old_by_expr[e].get("name") != new_by_expr[e].get("name")
    ]
    unchanged = len(new_by_expr) - len(renamed) - len(added)

    lines = [
        f"[metrics] 生成器拥有: 旧 {len(owned)} 条 → 新 {len(new_metrics)} 条 "
        f"(改名 {len(renamed)} / 删除 {len(removed)} / 新增 {len(added)} / 不变 {unchanged})"
    ]
    for e in sorted(removed):
        lines.append(f"  - {old_by_expr[e].get('name')!r}   [{e}]")
    for e in sorted(added):
        lines.append(f"  + {new_by_expr[e].get('name')!r}   [{e}]")
    for e in sorted(renamed):
        lines.append(f"  ~ {old_by_expr[e].get('name')!r}\n    → {new_by_expr[e].get('name')!r}   [{e}]")
    return "\n".join(lines)


def regen_metrics(kb: Path, lang: str) -> tuple[dict[str, Any], str, bool]:
    """→ (改后的文档, diff 文本, 是否有改动)。键集合与原文件保持一致。"""
    sem_path = kb / "semantics.yml"
    if not sem_path.exists():
        sys.exit(f"缺 {sem_path}")

    terms = generate_terms(_load_yaml(kb / "schema_notes.yml").get("tables") or [], lang=lang)
    new_metrics = terms_to_ossie_document(terms, kb.name)["semantic_model"][0]["metrics"]

    doc = _load_yaml(sem_path)
    model = (doc.get("semantic_model") or [{}])[0]
    old_metrics = model.get("metrics") or []
    datasets = {
        d.get("name", ""): {f.get("name", "") for f in (d.get("fields") or [])}
        for d in model.get("datasets") or []
    }

    owned = [mt for mt in old_metrics if _is_generator_owned(_expr_of(mt), datasets)]
    rebuilt, errors = _rebuild_metrics(old_metrics, new_metrics, datasets)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit("检测到人工加工的生成器条目——请人工处理后再跑")

    diff = _render_metric_diff(owned, new_metrics)
    if rebuilt == old_metrics:
        return doc, diff + "\n  (已在同步状态)", False
    model["metrics"] = rebuilt
    return doc, diff, True


# ── examples ─────────────────────────────────────────────


def _is_stale_generated(entry: dict[str, Any]) -> bool:
    """生成器签名的陈旧模板:单表聚合 SQL + ``[表, 列, aggregation]`` tags。"""
    tags = [str(t) for t in (entry.get("tags") or [])]
    sql = str(entry.get("sql") or "").strip()
    return (
        len(tags) == 3
        and tags[2] in ("aggregation", "聚合")
        and bool(_TEMPLATE_AGG_RE.match(sql))
    )


def regen_examples(kb: Path, lang: str) -> tuple[dict[str, Any], str, bool]:
    """→ (改后的文档, diff 文本, 是否有改动)。只动 template 条目且只动可证明的部分。"""
    ex_path = kb / "examples.yml"
    if not ex_path.exists():
        sys.exit(f"缺 {ex_path}")

    tables = _load_yaml(kb / "schema_notes.yml").get("tables") or []
    fresh = {
        str(t.get("sql", "")).strip(): t
        for t in generate_templates(tables, lang=lang)
    }
    doc = _load_yaml(ex_path)
    entries = doc.get("examples") or []

    aligned: list[tuple[str, str]] = []
    dropped: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    for entry in entries:
        sql = str(entry.get("sql") or "").strip()
        if not entry.get("template"):
            out.append(entry)  # 人工/自动捕获条目:一字不动
            continue
        tmpl = fresh.get(sql)
        if tmpl is not None:
            # 只有 tags 逐字相同才是生成器产物(合成条目可复用模板 SQL)。
            if list(entry.get("tags") or []) == list(tmpl.get("tags") or []):
                question = tmpl.get("question")
                if question and question != entry.get("question"):
                    aligned.append((str(entry.get("question")), str(question)))
                    entry = {**entry, "question": question}
            out.append(entry)
        elif _is_stale_generated(entry):
            dropped.append(entry)
        else:
            out.append(entry)  # LLM 合成(与确定性模板同标志,不重建)

    n_template = sum(1 for e in entries if e.get("template"))
    lines = [
        f"[examples] 条目 {len(entries)}(模板 {n_template}) → 问句对齐 {len(aligned)} / "
        f"陈旧剔除 {len(dropped)} / 保留 {len(out)}"
    ]
    for old_q, new_q in aligned:
        lines.append(f"  ~ {old_q!r}\n    → {new_q!r}")
    for entry in dropped:
        lines.append(f"  - {entry.get('question')!r}   [{str(entry.get('sql'))[:70]}]")
    if not aligned and not dropped:
        return doc, "\n".join(lines) + "\n  (已在同步状态)", False
    doc["examples"] = out
    return doc, "\n".join(lines), True


# ── CLI ──────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kb-dir", default=".trove/kb/financial", help="KB 目录(默认 financial)")
    ap.add_argument("--lang", default="en", help="命名语言(默认 en,与 KB 提问语言一致)")
    ap.add_argument("--only", choices=("metrics", "examples"), help="只跑其中一段")
    ap.add_argument("--write", action="store_true", help="落盘(默认只 dry-run 打印 diff)")
    ap.add_argument("--check", action="store_true", help="有漂移时退出码 1(不落盘)")
    args = ap.parse_args()

    kb = Path(args.kb_dir)
    targets = [args.only] if args.only else ["metrics", "examples"]
    pending: list[tuple[Path, dict[str, Any]]] = []

    for target in targets:
        path = kb / ("semantics.yml" if target == "metrics" else "examples.yml")
        raw = path.read_text(encoding="utf-8")
        doc = _load_yaml(path)
        if yaml.safe_dump(doc, **_DUMP_KWARGS) != raw:
            sys.exit(f"{path} YAML 往返非 byte-identical,拒绝重写")

        fn = regen_metrics if target == "metrics" else regen_examples
        doc, diff, changed = fn(kb, args.lang)
        print(diff)
        if changed:
            pending.append((path, doc))

    if not pending:
        print("全部已在同步状态,无改动")
        return 0
    if not args.write:
        print("(dry-run;加 --write 落盘)")
        return 1 if args.check else 0
    for path, doc in pending:
        path.write_text(yaml.safe_dump(doc, **_DUMP_KWARGS), encoding="utf-8")
        print(f"已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
