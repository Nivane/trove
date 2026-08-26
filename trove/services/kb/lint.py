"""Static checks over parsed KB entries — catch silent KB regressions.

Covers the failure modes observed when the deterministic kb init replaced
a hand-crafted KB:
  - term mappings referencing unknown columns
  - SUM/AVG over ID/account-number columns (e.g. SUM(account_to))
  - example SQL that doesn't parse, references unknown tables, or writes
  - empty column descriptions (e.g. district A-columns left blank)
  - lessons with unusable patterns/notes
  - Chinese-only example questions (unreachable by English retrieval)

All functions are pure: parsed entries in, issue message list out.
"""

from __future__ import annotations

import re
from typing import Any

from sqlglot import ErrorLevel, exp, parse_one

from trove.services.kb.lesson_distill import MAX_PATTERN_LEN

_ID_LIKE_SUFFIXES = ("_id", "_to", "_from", "_code", "id")


def parse_enum_values(enum_text: str) -> set[str]:
    """枚举文本 → 取值集合(兼容三种来源格式):

    - "A=含义" 分号列表(LLM 草稿/人工;全角冒号 "F：female" 同义)
    - "'A' stands for ..." BIRD value_description 原文(多行)
    - "VALUE1; VALUE2" 探测原始值(无 "=" 无引号 → 整段为值)
    """
    values: set[str] = set()
    for part in re.split(r"[;\n]", enum_text):
        entry = part.strip()
        if not entry:
            continue
        if "=" in entry or "：" in entry:
            key = entry.split("=" if "=" in entry else "：", 1)[0].strip()
        else:
            m = re.match(r"""['"]([^'"]+)['"]""", entry)
            key = m.group(1).strip() if m else entry
        if key:
            values.add(key)
    return values


def _parse(sql: str, dialect: str = "mysql") -> exp.Expression | None:
    """严格解析:语法错误返回 None(宽松模式下 "SELEC broken" 会被
    解析成 Alias 而非报错);调用方按需再判是否为查询语句。

    默认按 MySQL 解析(BIRD 评估目标);BIRD gold 的 DATE_FORMAT、
    CAST(x AS DOUBLE)、反引号限定列等写法通用方言会误报。
    """
    try:
        return parse_one(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
    except Exception:
        return None


def lint_terms(
    terms: list[dict[str, Any]],
    schema: dict[str, set[str]],
    dialect: str = "mysql",
) -> list[str]:
    """Term mappings must reference existing columns; no SUM/AVG on ID-like columns.

    schema 的列名与 mapping 按小写比较(MySQL 列名大小写不敏感,
    schema_notes 里 A5 与 mapping 里 a5 是同一列)。
    """
    schema = {t: {c.lower() for c in cols} for t, cols in schema.items()}
    issues: list[str] = []
    for term in terms:
        name = str(term.get("term", ""))
        mapping = str(term.get("mapping", ""))
        tree = _parse(mapping, dialect)
        if tree is None:
            issues.append(f"术语「{name}」mapping 无法解析: {mapping[:60]}")
            continue
        agg = next(tree.find_all(exp.Sum, exp.Avg), None)
        bound = [t for t in term.get("tables", []) if t]
        for col in tree.find_all(exp.Column):
            col_name = (col.name or "").lower()
            col_table = (col.table or "").lower()
            if col_table:
                if col_table not in schema or col_name not in schema[col_table]:
                    issues.append(
                        f"术语「{name}」mapping 引用不存在的列 {col_table}.{col_name}")
            elif bound:
                if not any(col_name in schema.get(t, set()) for t in bound):
                    issues.append(f"术语「{name}」mapping 引用绑定表中不存在的列 {col_name}")
            elif not any(col_name in cols for cols in schema.values()):
                issues.append(f"术语「{name}」mapping 引用不存在的列 {col_name}")
            if agg is not None and col_name.endswith(_ID_LIKE_SUFFIXES):
                issues.append(f"术语「{name}」对 ID/账号类列 {col_name} 做聚合,无业务含义")
    return issues


def lint_examples(
    examples: list[dict[str, Any]],
    tables: set[str],
    dialect: str = "mysql",
) -> list[str]:
    """Example SQL must parse, read only known tables; questions need English reachability."""
    issues: list[str] = []
    for example in examples:
        question = str(example.get("question", ""))
        sql = str(example.get("sql", ""))
        tree = _parse(sql, dialect)
        if tree is None:
            issues.append(f"示例「{question[:30]}」SQL 无法解析: {sql[:60]}")
            continue
        if any(isinstance(n, (exp.Insert, exp.Update, exp.Delete, exp.Drop))
               for n in tree.walk()):
            issues.append(f"示例「{question[:30]}」包含写操作,不应作为参考示例")
            continue
        if not isinstance(tree, exp.Query):
            issues.append(f"示例「{question[:30]}」SQL 无法解析(或不是查询语句): {sql[:60]}")
            continue
        for table in tree.find_all(exp.Table):
            if table.name and table.name not in tables:
                issues.append(f"示例「{question[:30]}」引用了不存在的表 {table.name}")
        if question and not re.search(r"[A-Za-z]", question):
            issues.append(
                f"示例问题「{question[:30]}」无英文内容,英文问题检索命中率低(建议双语)")
    return issues


def lint_semantics(model: dict[str, Any], dialect: str = "mysql") -> list[str]:
    """语义层模型(OSSIE):同名字段/指标、坏表达式、非法 alias、越界关系、
    metric 内建 filter / agg_time_dimension 声明一致性、M:N 与路径二义提前暴露。"""
    issues: list[str] = []
    datasets = list(model.get("datasets", []) or [])
    ds_fields: dict[str, set[str]] = {
        str(d.get("name", "")): {
            str(f.get("name", "")) for f in (d.get("fields", []) or [])
        }
        for d in datasets
    }
    temporal_fields: dict[str, set[str]] = {}
    for d in datasets:
        ds_name = str(d.get("name", ""))
        tmp = set()
        for f in d.get("fields", []) or []:
            if str(f.get("semantic_role", "") or "").lower() == "time":
                tmp.add(str(f.get("name", "")))
            elif str(f.get("datatype", "") or "").lower() in (
                "date", "time", "datetime", "datetimetz", "timestamp",
            ):
                tmp.add(str(f.get("name", "")))
        temporal_fields[ds_name] = tmp

    seen_metrics: set[str] = set()
    for m in model.get("metrics", []) or []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name", ""))
        if name and name in seen_metrics:
            issues.append(f"指标「{name}」重复定义")
        seen_metrics.add(name)
        for dia in (m.get("expression") or {}).get("dialects", []) or []:
            expr = str(dia.get("expression", ""))
            if expr and _parse(expr, dialect) is None:
                issues.append(f"指标「{name}」表达式无法解析: {expr[:60]}")
        _lint_metric_filter(issues, m, ds_fields, dialect)
        _lint_metric_agg_time(issues, m, ds_fields, temporal_fields)
        _lint_metric_non_additive(issues, m, model)

    ds_names = set(ds_fields)
    for d in datasets:
        if not isinstance(d, dict):
            continue
        ds_name = str(d.get("name", ""))
        seen_fields: set[str] = set()
        for f in d.get("fields", []) or []:
            if not isinstance(f, dict):
                continue
            fname = str(f.get("name", ""))
            if fname and fname in seen_fields:
                issues.append(f"表 {ds_name} 字段「{fname}」重复定义")
            seen_fields.add(fname)
            for syn in (f.get("ai_context") or {}).get("synonyms") or []:
                if not isinstance(syn, str) or not syn.strip():
                    issues.append(f"表 {ds_name}.{fname} 含空/非法 synonym")
            for dia in (f.get("expression") or {}).get("dialects", []) or []:
                expr = str(dia.get("expression", ""))
                if expr and _parse(expr, dialect) is None:
                    issues.append(f"表 {ds_name}.{fname} 表达式无法解析: {expr[:60]}")

    rel_pairs: set[frozenset[str]] = set()
    for r in model.get("relationships", []) or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("from", "")) not in ds_names or str(r.get("to", "")) not in ds_names:
            issues.append(f"关系「{r.get('name', '')}」引用未声明的数据集")
        if not str(r.get("cardinality") or "").strip():
            issues.append(f"关系「{r.get('name', '')}」未声明基数(cardinality),编译器将保守 MISS")
        if str(r.get("cardinality") or "").strip().upper() == "M:N":
            issues.append(
                f"关系「{r.get('name', '')}」为 M:N 多对多,编译期将拒绝 fan-out"
                "(如确属多对多,建议改为 1:N + 中间表显式建模)")
        rel_pairs.add(frozenset((str(r.get("from", "")), str(r.get("to", "")))))

    _lint_path_ambiguity(issues, model, ds_names)

    # 命名约定 FK → 已声明数据集 但未声明关系:编译器「matched 但连不上」
    # 会产出 FROM anchor 无 JOIN 的非法 SQL(被投影表守卫拦成 MISS)。
    # 在建模期暴露,别等查询期。
    for d in datasets:
        if not isinstance(d, dict):
            continue
        ds_name = str(d.get("name", ""))
        for f in d.get("fields", []) or []:
            if not isinstance(f, dict):
                continue
            fname = str(f.get("name", ""))
            if not fname.endswith("_id") or len(fname) <= 3:
                continue
            target = fname[:-3]
            if target == ds_name or target not in ds_names:
                continue
            if frozenset((ds_name, target)) not in rel_pairs:
                issues.append(
                    f"表 {ds_name}.{fname} 按命名约定指向已声明的 {target},"
                    "但 relationships 未声明这对关系(联表将 MISS)")
    return issues


def _lint_metric_filter(
    issues: list[str], m: dict[str, Any], ds_fields: dict[str, set[str]],
    dialect: str,
) -> None:
    """metric 内建 filter 必须可解析,且引用的列须落在 metric 数据集的声明字段内。"""
    ftext = str(m.get("filter") or "").strip()
    if not ftext:
        return
    name = str(m.get("name", ""))
    tree = _parse(ftext, dialect)
    if tree is None:
        issues.append(f"指标「{name}」filter 无法解析: {ftext[:60]}")
        return
    bound = [t for t in (m.get("datasets") or []) if t]
    for col in tree.find_all(exp.Column):
        col_name = (col.name or "").lower()
        col_table = (col.table or "").lower()
        if col_table:
            if col_table not in ds_fields or col_name not in ds_fields[col_table]:
                issues.append(
                    f"指标「{name}」filter 引用不存在的列 {col_table}.{col_name}")
        elif bound:
            if not any(col_name in ds_fields.get(t, set()) for t in bound):
                issues.append(
                    f"指标「{name}」filter 引用不在其数据集中的列 {col_name}")
        elif not any(col_name in cols for cols in ds_fields.values()):
            issues.append(f"指标「{name}」filter 引用不存在的列 {col_name}")


def _lint_metric_agg_time(
    issues: list[str], m: dict[str, Any], ds_fields: dict[str, set[str]],
    temporal_fields: dict[str, set[str]],
) -> None:
    """agg_time_dimension 必须是 metric 数据集内已声明的时间字段。"""
    ref = str(m.get("agg_time_dimension") or "").strip()
    if not ref:
        return
    name = str(m.get("name", ""))
    tbl = ref.split(".", 1)[0] if "." in ref else ""
    col = ref.split(".", 1)[1] if "." in ref else ref
    bound = [t for t in (m.get("datasets") or []) if t]
    if tbl:
        if tbl not in ds_fields or col not in ds_fields[tbl]:
            issues.append(f"指标「{name}」agg_time_dimension 引用不存在的列 {tbl}.{col}")
        elif col not in temporal_fields.get(tbl, set()):
            issues.append(f"指标「{name}」agg_time_dimension {tbl}.{col} 不是时间字段")
    else:
        cands = [t for t in (bound or list(ds_fields)) if col in ds_fields.get(t, set())]
        if len(cands) != 1:
            issues.append(
                f"指标「{name}」agg_time_dimension {col} 未唯一解析到声明字段")
        elif col not in temporal_fields.get(cands[0], set()):
            issues.append(
                f"指标「{name}」agg_time_dimension {cands[0]}.{col} 不是时间字段")


def _lint_metric_non_additive(
    issues: list[str], m: dict[str, Any], model: dict[str, Any],
) -> None:
    """non_additive 度量被其他度量表达式按名引用(再聚合)→ 警告。"""
    if not m.get("non_additive"):
        return
    name = str(m.get("name", ""))
    for other in model.get("metrics", []) or []:
        if not isinstance(other, dict):
            continue
        oname = str(other.get("name", ""))
        if oname == name:
            continue
        for dia in (other.get("expression") or {}).get("dialects", []) or []:
            if name.lower() in str(dia.get("expression", "")).lower():
                issues.append(
                    f"non_additive 指标「{name}」被指标「{oname}」的表达式引用,"
                    "可能造成重复计算/再聚合,请复核")
                return


def _lint_path_ambiguity(
    issues: list[str], model: dict[str, Any], ds_names: set[str],
) -> None:
    """声明关系图上两数据集之间存在多条简单路径 → 编译期可能 ambiguous MISS。

    运行时只在"被查询涉及"时才判二义;建模期提前暴露,让建模者把路径
    收敛(如只声明必要边)。图小,直接 BFS 计数简单路径(深度上限防爆)。
    """
    adj: dict[str, list[str]] = {}
    for r in model.get("relationships", []) or []:
        if not isinstance(r, dict):
            continue
        a, b = str(r.get("from", "")), str(r.get("to", ""))
        if a not in ds_names or b not in ds_names:
            continue
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    if not adj:
        return

    def paths(u: str, v: str, seen: frozenset[str], depth: int) -> int:
        if depth > 12:
            return 0
        if u == v:
            return 1
        n = 0
        for nb in adj.get(u, []):
            if nb in seen:
                continue
            n += paths(nb, v, seen | {u}, depth + 1)
            if n > 1:
                return n  # 只需知道 >1
        return n

    reported: set[frozenset[str]] = set()
    nodes = sorted(adj)
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            if frozenset((a, b)) in reported:
                continue
            if paths(a, b, frozenset(), 0) > 1:
                reported.add(frozenset((a, b)))
                issues.append(
                    f"关系图上 {a}↔{b} 存在多条简单路径,编译器将拒绝二义"
                    "(ambigous_join_path)——请收敛建模(仅声明必要边)")


def lint_tables(tables: list[dict[str, Any]]) -> list[str]:
    """Columns with empty descriptions are knowledge blind spots.

    入参与 _parse_file 的 schema_notes payload 同形:columns 为
    {列名: 描述} 字典,表名在 "name" 字段。
    """
    issues: list[str] = []
    for table in tables:
        table_name = str(table.get("name", ""))
        for col_name, desc in (table.get("columns") or {}).items():
            if not str(desc or "").strip():
                issues.append(
                    f"表 {table_name} 的列 {col_name} 描述为空"
                    "(盲区,建议从官方文档导入或标注含义未知)")
    return issues


_STATS_KEYS = {
    "null_ratio", "distinct", "min", "max", "min_len", "max_len", "shape",
    "top_values", "sample",
}
_SHAPES = {"numeric", "json", "composite", "all_caps", "capital", "text"}


def lint_stats(tables: list[dict[str, Any]]) -> list[str]:
    """Profiling stats 格式校验:未知键、取值域、形状枚举。

    tables 形如 _parse_file 的 table payload:{"name", "stats": {列名: dict}}。
    统计是 LLM 描述的证据源,格式错了会静默污染 schema_context。
    """
    issues: list[str] = []
    for table in tables:
        table_name = str(table.get("name", ""))
        for col_name, st in (table.get("stats") or {}).items():
            if not isinstance(st, dict):
                issues.append(f"表 {table_name} 列 {col_name} stats 不是对象")
                continue
            unknown = set(st) - _STATS_KEYS
            if unknown:
                issues.append(
                    f"表 {table_name} 列 {col_name} stats 含未知键: {sorted(unknown)}")
            nr = st.get("null_ratio")
            if nr is not None and not (
                isinstance(nr, (int, float)) and 0 <= nr <= 1
            ):
                issues.append(f"表 {table_name} 列 {col_name} null_ratio 超出 [0,1]: {nr}")
            d = st.get("distinct")
            if d is not None and not (isinstance(d, int) and d >= 0):
                issues.append(f"表 {table_name} 列 {col_name} distinct 非法: {d}")
            mn, mx = st.get("min_len"), st.get("max_len")
            if mn is not None and mx is not None and mn > mx:
                issues.append(f"表 {table_name} 列 {col_name} min_len > max_len")
            shape = st.get("shape")
            if shape is not None and shape not in _SHAPES:
                issues.append(f"表 {table_name} 列 {col_name} shape 未知: {shape}")
            tv = st.get("top_values")
            if tv is not None:
                if (
                    not isinstance(tv, list)
                    or not tv
                    or any(
                        not isinstance(p, list) or len(p) != 2 or not isinstance(p[1], int)
                        for p in tv
                    )
                ):
                    issues.append(
                        f"表 {table_name} 列 {col_name} top_values 非法"
                        "(应为 [[值, 频次], ...])")
            sample = st.get("sample")
            if sample is not None and (
                not isinstance(sample, list) or not sample
                or any(v is None for v in sample)
            ):
                issues.append(
                    f"表 {table_name} 列 {col_name} sample 非法"
                    "(应为非空取值列表)")
    return issues


def lint_lessons(lessons: list[dict[str, Any]]) -> list[str]:
    """Lessons must carry a short, retrievable pattern and a non-empty note."""
    issues: list[str] = []
    for lesson in lessons:
        pattern = str(lesson.get("pattern", "") or "").strip()
        note = str(lesson.get("note", "") or "").strip()
        if len(pattern) > MAX_PATTERN_LEN:
            issues.append(
                f"lesson pattern 过长({len(pattern)}>{MAX_PATTERN_LEN}): {pattern[:40]}…")
        if not note:
            issues.append(f"lesson「{pattern[:30]}」note 为空")
    return issues
