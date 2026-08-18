"""Enum & date-range probing — fill KB schema_notes from the live datasource.

低基数文本列(如 account.frequency)的 distinct 取值是术语映射的关键
上下文("周发放 = POPLATEK TYDNE"就地可见,不必依赖 evidence 兜底)。
probe_enums 对每张表的文本列执行 `SELECT DISTINCT col ... LIMIT N+1`
(带行数护栏与超时),distinct ≤ N 的列记入 enums;scripts/probe_enums.py
一次性写入 schema_notes.yml。人工已写的枚举含义默认不被覆盖。

日期列(BIRD 为 YYMMDD 文本或 YYYY-MM-DD)的 distinct 通常超过 limit,
distinct 路径无意义——probe_date_ranges 对日期类型列(以及 distinct
超限的文本列)执行 `SELECT MIN(col), MAX(col)` 拿值域,写入列的
`range` 字段;deterministic_gen 据此生成年份/区间/比较模板。
"""

from __future__ import annotations

import asyncio
import copy
import re
from typing import Any

from trove.services.kb.lint import parse_enum_values

PROBE_LIMIT = 20            # distinct 取值 ≤ 此数才记入枚举
DEFAULT_MAX_ROWS = 2_000_000  # 行数护栏:超大表(如 1M 交易表)也允许探测
DEFAULT_TIMEOUT_S = 20        # 单列探测超时(慢列静默跳过)

_TEXT_TYPE_MARKERS = ("char", "text", "enum")
_DATE_TYPE_MARKERS = ("date", "datetime", "timestamp", "time")

# 像日期的值:YYMMDD / YYYYMMDD / YYYY-MM-DD / YYYY-MM-DD HH:MM:SS
_DATE_VALUE_RE = re.compile(
    r"^(\d{6}|\d{8}|\d{4}-\d{2}-\d{2})([\sT].*)?$")


async def probe_enums(
    registry: Any,
    schema: Any,
    *,
    limit: int = PROBE_LIMIT,
    max_rows: int = DEFAULT_MAX_ROWS,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> dict[str, dict[str, str]]:
    """Probe distinct values of low-cardinality text columns.

    Returns:
        {table: {column: "v1; v2; ..."}} — only columns with 1..limit
        distinct values; probe failures/timeouts are silently skipped.
    """
    results: dict[str, dict[str, str]] = {}
    for table in schema.tables:
        estimate = table.row_count_estimate or 0
        if estimate and estimate > max_rows:
            continue
        text_cols = [
            c for c in table.columns
            if any(m in (c.type or "").lower() for m in _TEXT_TYPE_MARKERS)
        ]
        for col in text_cols:
            sql = f"SELECT DISTINCT `{col.name}` FROM `{table.name}` LIMIT {limit + 1}"
            try:
                res = await asyncio.wait_for(registry.execute(sql), timeout=timeout_s)
            except Exception:
                continue
            values = [r[0] for r in res.rows if r and r[0] is not None]
            if not values or len(values) > limit:
                continue
            results.setdefault(table.name, {})[col.name] = "; ".join(
                str(v) for v in values
            )
    return results


async def probe_date_ranges(
    registry: Any,
    schema: Any,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> dict[str, dict[str, list[str]]]:
    """Probe [MIN, MAX] value ranges of date-ish columns.

    Candidates: columns whose type carries a date marker, PLUS text
    columns whose distinct count exceeds PROBE_LIMIT (BIRD stores
    YYMMDD dates as TEXT — the distinct path silently skips them).
    Each candidate gets `SELECT MIN(c), MAX(c)`; a range is recorded
    only when both ends are non-empty and look like dates.

    Returns {table: {column: [min, max]}}; failures/timeouts skipped.
    """
    results: dict[str, dict[str, list[str]]] = {}
    for table in schema.tables:
        estimate = table.row_count_estimate or 0
        if estimate and estimate > max_rows:
            continue
        for col in table.columns:
            col_type = (col.type or "").lower()
            date_marked = any(m in col_type for m in _DATE_TYPE_MARKERS)
            text_fallback = (
                any(m in col_type for m in _TEXT_TYPE_MARKERS)
                and not date_marked
            )
            if not (date_marked or text_fallback):
                continue
            # 文本回退列:distinct ≤ limit 时已有枚举路径覆盖,不重复探测
            if text_fallback:
                try:
                    res = await asyncio.wait_for(
                        registry.execute(
                            f"SELECT DISTINCT `{col.name}` FROM `{table.name}` "
                            f"LIMIT {PROBE_LIMIT + 1}"
                        ),
                        timeout=timeout_s,
                    )
                except Exception:
                    continue
                distinct = [r[0] for r in res.rows if r and r[0] is not None]
                if distinct and len(distinct) <= PROBE_LIMIT:
                    continue
            try:
                res = await asyncio.wait_for(
                    registry.execute(
                        f"SELECT MIN(`{col.name}`), MAX(`{col.name}`) "
                        f"FROM `{table.name}`"
                    ),
                    timeout=timeout_s,
                )
            except Exception:
                continue
            if not res.rows:
                continue
            lo, hi = res.rows[0][0], res.rows[0][1]
            if lo is None or hi is None:
                continue
            lo, hi = str(lo).strip(), str(hi).strip()
            if not (_DATE_VALUE_RE.match(lo) and _DATE_VALUE_RE.match(hi)):
                continue
            results.setdefault(table.name, {})[col.name] = [lo, hi]
    return results


def merge_into_notes(
    notes: dict[str, Any],
    probed: dict[str, dict[str, str]],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Fill probed enum values into a schema_notes.yml structure.

    Non-destructive by default: columns that already carry enum notes
    (人工写好的取值含义) keep their entries, and only probed values
    NOT already mentioned are appended as bare values (含义未知也好过
    取值在 KB 里不存在——BIRD 官方 value_description 常漏取值)。
    Returns a new dict; the caller's structure is not mutated.
    """
    merged = copy.deepcopy(notes)
    tables = merged.setdefault("tables", [])
    by_name = {str(t["name"]): t for t in tables}
    for table_name, cols in probed.items():
        table = by_name.get(table_name)
        if table is None:
            continue
        col_by_name = {
            str(c["name"]): c for c in table.setdefault("columns", [])
        }
        for col_name, values in cols.items():
            col = col_by_name.get(col_name)
            if col is None:
                continue
            existing = [
                str(e).strip() for e in (col.get("enums") or []) if str(e).strip()
            ]
            if overwrite or not existing:
                col["enums"] = values.split("; ")
                continue
            known = set()
            for entry in existing:
                known |= parse_enum_values(entry)
            missing = [v for v in values.split("; ") if v and v not in known]
            if missing:
                col["enums"] = existing + missing
    return merged


def merge_ranges_into_notes(
    notes: dict[str, Any],
    ranges: dict[str, dict[str, list[str]]],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Fill probed [min, max] ranges into schema_notes date columns.

    Non-destructive by default: columns that already carry a `range`
    (人工写入或上次探测) keep it unless overwrite=True. Columns get
    `range: [min, max]`; the enums field is left untouched (date values
    are not enum codes).
    """
    merged = copy.deepcopy(notes)
    tables = merged.setdefault("tables", [])
    by_name = {str(t["name"]): t for t in tables}
    for table_name, cols in ranges.items():
        table = by_name.get(table_name)
        if table is None:
            continue
        col_by_name = {
            str(c["name"]): c for c in table.setdefault("columns", [])
        }
        for col_name, value_range in cols.items():
            col = col_by_name.get(col_name)
            if col is None:
                continue
            if overwrite or "range" not in col or not col.get("range"):
                col["range"] = [str(v) for v in value_range]
    return merged
