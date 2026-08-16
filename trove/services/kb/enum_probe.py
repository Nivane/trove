"""Enum value probing — fill KB schema_notes enums from the live datasource.

低基数文本列(如 account.frequency)的 distinct 取值是术语映射的关键
上下文("周发放 = POPLATEK TYDNE"就地可见,不必依赖 evidence 兜底)。
probe_enums 对每张表的文本列执行 `SELECT DISTINCT col ... LIMIT N+1`
(带行数护栏与超时),distinct ≤ N 的列记入 enums;scripts/probe_enums.py
一次性写入 schema_notes.yml。人工已写的枚举含义默认不被覆盖。
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

PROBE_LIMIT = 20            # distinct 取值 ≤ 此数才记入枚举
DEFAULT_MAX_ROWS = 2_000_000  # 行数护栏:超大表(如 1M 交易表)也允许探测
DEFAULT_TIMEOUT_S = 20        # 单列探测超时(慢列静默跳过)

_TEXT_TYPE_MARKERS = ("char", "text", "enum")


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


def merge_into_notes(
    notes: dict[str, Any],
    probed: dict[str, dict[str, str]],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Fill probed enum values into a schema_notes.yml structure.

    Non-destructive by default: columns that already carry enum notes
    (人工写好的取值含义) are left untouched unless overwrite=True.
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
            if existing and not overwrite:
                continue
            col["enums"] = values.split("; ")
    return merged
