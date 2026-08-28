"""Parameterized SQL templates — deterministic {{var}} analysis.

参考 SQL 模板(``examples.yml`` 里 ``template: true`` 且 SQL 含 ``{{var}}``)
在注入 gen_sql 上下文时,参数经**确定性静态分析**(零 LLM)分类并解析到
声明列,再用语义模型里的枚举值丰富样例值——让 LLM 看到"可复用形状 +
合法取值",而不是把 ``{{var}}`` 当字面量抄进 SQL。

分类规则(与 Datus reference_template 同思路):
  - ``'{{p}}'`` 引号内 → dimension(过滤值;经 sqlglot 静态分析解析到
    ``table.column``);
  - ``LIMIT {{p}}`` → number;
  - ``ORDER BY ... {{p}}`` → keyword(ASC/DESC);
  - ``(比较符) {{p}}`` → number;
  - 其余裸位置(SELECT/GROUP BY 标识符)→ column。
"""

from __future__ import annotations

import re
from typing import Any

_PARAM_RE = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}")
_QUOTED_PARAM_RE = re.compile(r"'\{\{\s*([A-Za-z_]\w*)\s*\}\}'")
_LIMIT_PARAM_RE = re.compile(r"\bLIMIT\s+\{\{\s*([A-Za-z_]\w*)\s*\}\}", re.I)
_ORDERBY_PARAM_RE = re.compile(
    r"\bORDER\s+BY\b[^;]*?\{\{\s*([A-Za-z_]\w*)\s*\}\}", re.I)
_CMP_PARAM_RE = re.compile(
    r"(?:>=|<=|<>|!=|=|>|<)\s*\{\{\s*([A-Za-z_]\w*)\s*\}\}", re.I)

# 引号内的 {{var}} 替换成哨兵字面量(静态解析定位过滤列)
_QUOTED_SUB_RE = re.compile(r"'\{\{\s*([A-Za-z_]\w*)\s*\}\}'")
# 其余 {{var}} 替换成合法标识符上下文(ASC,便于 sqlglot 解析)
_BARE_SUB_RE = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}")


def extract_params(template_sql: str) -> list[dict[str, str]]:
    """按首次出现顺序列出模板参数名(去重)。"""
    names: list[str] = []
    seen: set[str] = set()
    for m in _PARAM_RE.finditer(template_sql or ""):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            names.append(name)
    return [{"name": n} for n in names]


def _classify(template_sql: str, params: list[dict[str, str]]) -> dict[str, str]:
    """参数名 → 类型(dimension/number/keyword/column)。"""
    quoted = set(_QUOTED_PARAM_RE.findall(template_sql or ""))
    limited = set(_LIMIT_PARAM_RE.findall(template_sql or ""))
    ordered = set(_ORDERBY_PARAM_RE.findall(template_sql or ""))
    compared = set(_CMP_PARAM_RE.findall(template_sql or ""))
    out: dict[str, str] = {}
    for p in params:
        name = p["name"]
        if name in quoted:
            out[name] = "dimension"
        elif name in limited or name in compared:
            out[name] = "number"
        elif name in ordered:
            out[name] = "keyword"
        else:
            out[name] = "column"
    return out


def _resolve_dimension_columns(
    template_sql: str, params: list[dict[str, str]],
) -> dict[str, str]:
    """dimension 参数 → 声明列(``table.column``)。

    把 ``'{{p}}'`` 替换为哨兵字面量、其余替换为 ASC,sqlglot 解析后遍历
    EQ 节点:一侧是哨兵字面量、另一侧是 Column → 该列即过滤列。失败/不可
    解析 → 空(不阻塞,无列也能注入类型)。
    """
    sub = _QUOTED_SUB_RE.sub(lambda m: f"'__P_{m.group(1)}__'", template_sql or "")
    sub = _BARE_SUB_RE.sub("ASC", sub)
    try:
        from sqlglot import exp, parse_one

        tree = parse_one(sub)
    except Exception:
        return {}
    mapping: dict[str, str] = {}
    for eq in tree.find_all(exp.EQ):
        sentinel: str | None = None
        col: exp.Column | None = None
        for side in (eq.left, eq.right):
            if isinstance(side, exp.Column):
                col = side
            for lit in (side.find_all(exp.Literal) if hasattr(side, "find_all") else []):
                if isinstance(lit.this, str) and lit.this.startswith("__P_") and lit.this.endswith("__"):
                    sentinel = lit.this[4:-2]
        if sentinel and col is not None:
            tbl = col.table or ""
            mapping[sentinel] = f"{tbl}.{col.name}" if tbl else col.name
    return mapping


def _enrich_values(
    params: list[dict[str, str]],
    columns: dict[str, str],
    model: Any | None,
) -> list[dict[str, Any]]:
    """dimension 参数用语义模型枚举值丰富 sample_values(零 LLM)。"""
    values_by_col: dict[str, list[str]] = {}
    if model is not None:
        try:
            for d in model.datasets:
                for f in d.fields:
                    if not getattr(f, "enum_display", None):
                        continue
                    col = f"{d.name}.{f.name}"
                    labels = [str(v) for v in f.enum_display.values() if str(v)]
                    if labels:
                        values_by_col[col] = labels
        except Exception:
            pass
    out: list[dict[str, Any]] = []
    for p in params:
        name = p["name"]
        entry: dict[str, Any] = {"name": name, "type": p["type"]}
        col = columns.get(name, "")
        if col:
            entry["column"] = col
            if col in values_by_col:
                entry["sample_values"] = values_by_col[col][:8]
        out.append(entry)
    return out


def analyze_template(
    template_sql: str, model: Any | None = None,
) -> list[dict[str, Any]]:
    """一条模板 SQL → 参数列表(类型/列/样例值)。无参数 → []。"""
    params = extract_params(template_sql)
    if not params:
        return []
    types = _classify(template_sql, params)
    columns = _resolve_dimension_columns(template_sql, params)
    return _enrich_values(
        [{"name": p["name"], "type": types.get(p["name"], "column")} for p in params],
        columns, model,
    )
