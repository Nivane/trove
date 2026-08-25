"""Dialect-aware time-grain bucketing for the semantic compiler.

``date_trunc`` 在四方言下的等价表达式(编译进投影 + GROUP BY):
sqlite/mysql 产 TEXT 标签(同年内字典序 = 时间序),duckdb/clickhouse 产
DATE 值;标签在同年内可排序,跨年不做 ORDER BY 保证(文档注明)。
未知方言回退 duckdb 式 ``date_trunc('grain', expr)``(postgres 兼容,
最常见默认)。
"""

from __future__ import annotations

from trove.services.semantic_layer.plan import GRAINS

_SQLITE = {
    "year": "strftime('%Y', {e})",
    "quarter": (
        "printf('%04d-Q%d', CAST(strftime('%Y', {e}) AS INTEGER), "
        "(CAST(strftime('%m', {e}) AS INTEGER) + 2) / 3)"
    ),
    "month": "strftime('%Y-%m', {e})",
    "week": "strftime('%Y-W%W', {e})",
    "day": "strftime('%Y-%m-%d', {e})",
}

_MYSQL = {
    "year": "DATE_FORMAT({e}, '%Y')",
    "quarter": "CONCAT(YEAR({e}), '-Q', QUARTER({e}))",
    "month": "DATE_FORMAT({e}, '%Y-%m')",
    "week": "DATE_FORMAT({e}, '%x-W%v')",
    "day": "DATE_FORMAT({e}, '%Y-%m-%d')",
}

_DUCKDB = {g: "date_trunc('{g}', {{e}})".format(g=g) for g in GRAINS}

_CLICKHOUSE = {
    "year": "toStartOfYear({e})",
    "quarter": "toStartOfQuarter({e})",
    "month": "toStartOfMonth({e})",
    "week": "toStartOfWeek({e})",
    "day": "toStartOfDay({e})",
}

_TABLES = {
    "sqlite": _SQLITE,
    "mysql": _MYSQL,
    "duckdb": _DUCKDB,
    "clickhouse": _CLICKHOUSE,
}


def date_trunc(expr: str, grain: str, dialect: str) -> str:
    """字段表达式 → 该方言下的分桶表达式(纯字符串模板,确定性)。"""
    table = _TABLES.get((dialect or "").strip().lower(), _DUCKDB)
    return table[grain].format(e=expr)
