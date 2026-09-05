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
    "doris": _MYSQL,  # Doris 兼容 MySQL 日期函数
    "duckdb": _DUCKDB,
    "clickhouse": _CLICKHOUSE,
}


def date_trunc(expr: str, grain: str, dialect: str) -> str:
    """字段表达式 → 该方言下的分桶表达式(纯字符串模板,确定性)。"""
    table = _TABLES.get((dialect or "").strip().lower(), _DUCKDB)
    return table[grain].format(e=expr)


# ── Time spine(时间轴):缺期补全的周期序列 ─────────────────────
#
# 模型声明 ``time_spine`` 且查询带时间分桶 + 可推导的时间范围时,编译器把
# 聚合结果 LEFT JOIN 到一张按 grain 生成的密集周期表,空档按 fill 策略补全
# (0 / previous / none)。spine 的 period 标签与 date_trunc 产物一致
# (同一分桶函数套在生成的日期点上),保证 ``t._period = spine.period`` 匹配。

# 安全上限:序列按「日」生成再分桶(跨 grain 步长统一、逻辑简单)。
# 上限 40_000 天 ≈ 109 年,超界拒绝(调用方回退无 spine)。
_SPINE_MAX_DAYS = 40_000


def _series(dialect: str, count: int) -> str:
    """生成 0..count-1 整数序列的子查询(各方言等价物)。"""
    if dialect == "clickhouse":
        return f"SELECT number AS n FROM numbers({int(count)})"
    if dialect in ("postgres", "duckdb"):
        return f"SELECT generate_series(0, {int(count) - 1}) AS n"
    # sqlite / mysql / doris:递归 CTE 在派生表内合法
    return (
        f"WITH RECURSIVE _seq(n) AS (SELECT 0 UNION ALL "
        f"SELECT n + 1 FROM _seq WHERE n < {int(count) - 1}) "
        f"SELECT n FROM _seq"
    )


def _date_add(dialect: str, lo: str, n_expr: str) -> str:
    """lo 日期 + n 天(按日推进,步长跨 grain 统一)。"""
    d = (dialect or "").strip().lower()
    if d == "clickhouse":
        return f"toDate('{lo}') + {n_expr}"
    if d in ("mysql", "doris"):
        return f"DATE_ADD(DATE('{lo}'), INTERVAL {n_expr} DAY)"
    if d == "sqlite":
        return f"date('{lo}', '+' || {n_expr} || ' days')"
    return f"(DATE '{lo}' + {n_expr})"  # postgres / duckdb


def time_spine_periods(lo: str, hi: str, grain: str, dialect: str) -> str | None:
    """lo..hi 间的密集周期子查询(``period`` 列)。

    返回可直接作 FROM 源(别名 spine)的子查询 SQL;范围超安全上限或
    lo>hi → None(调用方回退无 spine)。
    """
    from datetime import date as _date

    try:
        lo_d = _date.fromisoformat(lo)
        hi_d = _date.fromisoformat(hi)
    except ValueError:
        return None
    if hi_d < lo_d:
        return None
    day_count = (hi_d - lo_d).days + 1
    if day_count > _SPINE_MAX_DAYS:
        return None
    n = "n"
    bucket = date_trunc(_date_add(dialect, lo, n), grain, dialect)
    return f"SELECT DISTINCT {bucket} AS period\nFROM (\n{_series(dialect, day_count)}\n)"


def spine_fill_expr(col: str, fill: str) -> str:
    """缺期填充:0 → COALESCE;previous → 窗口 LAG;none → 原样。"""
    f = (fill or "none").strip().lower()
    if f == "0":
        return f"COALESCE({col}, 0)"
    if f == "previous":
        return (
            f"COALESCE({col}, LAG({col}) OVER (ORDER BY spine.period))"
        )
    return col
