"""EXPLAIN 行数估算守卫 (fail-open)。

执行前用 ``EXPLAIN``(规划,不取数)估算查询最重算子的行数,超上限
(``explain_max_rows``)打回 gen_sql 加 LIMIT/收窄过滤——防分析 agent
误跑超大扫描/笛卡尔积。

原则:
  - **fail-open**:方言不可解析 / EXPLAIN 失败 / 无估算 → 返回 None,放行。
    守卫是纵深防御的体验层,不是安全边界(真正的边界在数据库侧
    只读角色 + LIMIT/LEAST)。
  - 估算 = 各算子行数的最大值(最重算子),单表/单库不区分方言都能覆盖
    超大扫描;不对 MySQL 做乘积(乘积语义不稳,且最大值已拦最重单表)。
"""

from __future__ import annotations

import re
from typing import Any

from trove.core.types import QueryResult

# postgres: Seq Scan on loan (cost=... rows=1000 width=...)
_PG_ROWS_RE = re.compile(r"\brows=(\d+)", re.I)
# duckdb: Cardinality: 1000 / EC: 1000(EXPLAIN 逻辑/物理计划文本)
_DUCKDB_ROWS_RE = re.compile(r"\b(?:Cardinality|EC)\s*[:=]\s*(\d+)", re.I)


def _max_regex(rows: list[tuple], pattern: re.Pattern) -> int | None:
    """扫每行文本取行数最大值;无命中 → None。"""
    best: int | None = None
    for row in rows or []:
        for cell in row:
            if cell is None:
                continue
            m = pattern.search(str(cell))
            if m:
                n = int(m.group(1))
                best = n if best is None else max(best, n)
    return best


def _mysql_rows(rows: list[tuple], columns: list[str]) -> int | None:
    """MySQL EXPLAIN 表格:取 ``rows`` 列的最大值(最重表扫描)。"""
    if not columns or "rows" not in [str(c).lower() for c in columns]:
        return None
    idx = [str(c).lower() for c in columns].index("rows")
    best: int | None = None
    for row in rows or []:
        if idx >= len(row):
            continue
        cell = row[idx]
        if cell is None:
            continue
        try:
            n = int(cell)
        except (TypeError, ValueError):
            continue
        best = n if best is None else max(best, n)
    return best


# 方言 → 行数解析器(未列出的方言 → None = fail-open)
_PARSERS: dict[str, Any] = {
    "postgres": lambda rows, cols: _max_regex(rows, _PG_ROWS_RE),
    "postgresql": lambda rows, cols: _max_regex(rows, _PG_ROWS_RE),
    "duckdb": lambda rows, cols: _max_regex(rows, _DUCKDB_ROWS_RE),
    "mysql": _mysql_rows,
}


def estimate_max_rows(dialect: str, result: QueryResult) -> int | None:
    """EXPLAIN 结果 → 最重算子行数估算;无法解析 → None(fail-open)。"""
    parser = _PARSERS.get((dialect or "").strip().lower())
    if parser is None:
        return None
    try:
        return parser(result.rows, result.columns or [])
    except Exception:
        return None


def is_over_limit(dialect: str, result: QueryResult, max_rows: int) -> bool:
    """估算是否超限;无法估算(fail-open)或未超 → False(放行)。"""
    est = estimate_max_rows(dialect, result)
    if est is None:
        return False
    return est > max_rows
