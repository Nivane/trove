"""Statistical profiling — per-table/column statistics into schema_notes stats.

AskData 式 Database Profiling 的核心项:精确行数、null 比例、distinct 数、
min/max(数值/日期列)、值长度(文本列)、值形状检测(全数值/JSON/复合值/
全大写/首字母大写)。探测带超时与行数护栏,失败静默跳过——和 enum_probe
同一套护栏模式。

probe_stats 返回:
    {table: {"row_count": int,
             "columns": {col: {"null_ratio": float|None,
                               "distinct": int,
                               "min": ...|None, "max": ...|None,   # 数值/日期列
                               "min_len": int|None, "max_len": int|None,  # 文本列
                               "shape": str|None}}}                  # 文本列

merge_into_stats 把探测结果写入 schema_notes.yml 结构(非破坏性,默认
不覆盖已有 stats)。/kb init 调用链:probe_stats → merge_into_stats →
LLM 起草描述时统计证据随 schema_text 进入提示词。
"""

from __future__ import annotations

import asyncio
import copy
import re
from typing import Any

from trove.services.kb.enum_probe import DEFAULT_MAX_ROWS, DEFAULT_TIMEOUT_S

SHAPE_SAMPLE_LIMIT = 50  # 值形状检测的采样行数

# 类型判定标记(与 enum_probe / deterministic_gen 各自维护的元组同源)
_TEXT_TYPES = ("char", "text", "varchar", "enum", "string", "character")
_NUMERIC_TYPES = (
    "int", "integer", "bigint", "smallint", "tinyint", "float",
    "double", "decimal", "numeric", "real",
)
_DATE_TYPES = ("date", "datetime", "timestamp", "time")

_JSON_RE = re.compile(r"^\s*[\{\[]")
_SEPARATOR_RE = re.compile(r"[,;|#]")
_NUMERIC_RE = re.compile(
    r"^-?\d+(?:[.,]\d+)?$"  # 整数/小数,含千分位逗号或小数点
)

# 形状规则判定阈值:多数样本符合才判定,防止个别脏值误判
_JSON_MAJORITY = 0.5
_COMPOSITE_MAJORITY = 0.25


def detect_shape(samples: list[Any]) -> str | None:
    """从采样值推断列形状(纯函数,可直接单测)。

    判定顺序(先到先得):
      numeric    — 所有样本都是数字(整数/小数/千分位)
      json       — ≥一半样本以 { 或 [ 开头(JSON 编码字段)
      composite  — ≥1/4 样本含分隔符(逗号/分号/竖线/#)——复合值字段
      all_caps   — 所有样本(≥2 字符)全大写——枚举/编码列
      capital    — 所有样本首字母大写(英文专名,如地名)
      text       — 其余

    Returns:
        形状名;无样本(全 NULL/空表)返回 None。
    """
    values = [str(s).strip() for s in samples if s is not None and str(s).strip()]
    if not values:
        return None

    if all(_NUMERIC_RE.match(v) for v in values):
        return "numeric"

    json_fraction = sum(1 for v in values if _JSON_RE.match(v)) / len(values)
    if json_fraction >= _JSON_MAJORITY:
        return "json"

    composite_fraction = sum(1 for v in values if _SEPARATOR_RE.search(v)) / len(values)
    if composite_fraction >= _COMPOSITE_MAJORITY:
        return "composite"

    letters = [v for v in values if v.isalpha() or " " in v]
    if letters and all(v.isupper() and len(v) >= 2 for v in letters):
        return "all_caps"
    if letters and all(v.istitle() for v in letters):
        return "capital"

    return "text"


async def _probe_column(
    registry: Any, table: str, col: Any, timeout_s: int,
) -> dict[str, Any] | None:
    """单列统计:聚合查询一次拿总数/null 数/distinct + 极值或长度。

    文本列再补一条采样查询做值形状检测。探测失败/超时 → None(静默跳过)。
    """
    name = col.name
    col_type = (col.type or "").lower()

    if any(m in col_type for m in _TEXT_TYPES):
        select = (
            f"COUNT(*), SUM(`{name}` IS NULL), COUNT(DISTINCT `{name}`), "
            f"MIN(LENGTH(`{name}`)), MAX(LENGTH(`{name}`))"
        )
    elif any(m in col_type for m in (*_NUMERIC_TYPES, *_DATE_TYPES)):
        select = (
            f"COUNT(*), SUM(`{name}` IS NULL), COUNT(DISTINCT `{name}`), "
            f"MIN(`{name}`), MAX(`{name}`)"
        )
    else:
        select = f"COUNT(*), SUM(`{name}` IS NULL), COUNT(DISTINCT `{name}`)"

    sql = f"SELECT {select} FROM `{table}`"
    try:
        res = await asyncio.wait_for(registry.execute(sql), timeout=timeout_s)
    except Exception:
        return None
    row = res.rows[0] if res.rows else []
    if not row or len(row) < 3:
        return None
    total, nulls = int(row[0] or 0), int(row[1] or 0)
    stats: dict[str, Any] = {
        "null_ratio": round(nulls / total, 3) if total else None,
        "distinct": int(row[2] or 0),
    }

    if any(m in col_type for m in _TEXT_TYPES):
        if len(row) >= 5 and row[3] is not None:
            stats["min_len"] = int(row[3])
            stats["max_len"] = int(row[4])
        shape = await _probe_shape(registry, table, name, timeout_s)
        if shape:
            stats["shape"] = shape
    elif any(m in col_type for m in (*_NUMERIC_TYPES, *_DATE_TYPES)):
        if len(row) >= 5:
            stats["min"] = row[3]
            stats["max"] = row[4]
    return stats


async def _probe_shape(
    registry: Any, table: str, column: str, timeout_s: int,
) -> str | None:
    """采样前 N 行非 NULL 值 → detect_shape;失败返回 None。"""
    sql = (
        f"SELECT `{column}` FROM `{table}` "
        f"WHERE `{column}` IS NOT NULL LIMIT {SHAPE_SAMPLE_LIMIT}"
    )
    try:
        res = await asyncio.wait_for(registry.execute(sql), timeout=timeout_s)
    except Exception:
        return None
    return detect_shape([r[0] for r in res.rows if r])


async def probe_stats(
    registry: Any,
    schema: Any,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> dict[str, dict[str, Any]]:
    """对每张表执行统计探测(带行数护栏与超时,失败静默跳过)。

    Returns:
        {table: {"row_count": int, "columns": {col: stats_dict}}}。
    """
    results: dict[str, dict[str, Any]] = {}
    for table in schema.tables:
        estimate = table.row_count_estimate or 0
        if estimate and estimate > max_rows:
            continue  # 超大表跳过(护栏,同 enum_probe)
        try:
            res = await asyncio.wait_for(
                registry.execute(f"SELECT COUNT(*) FROM `{table.name}`"),
                timeout=timeout_s,
            )
        except Exception:
            continue
        row_count = int(res.rows[0][0]) if res.rows and res.rows[0] else 0
        columns: dict[str, dict[str, Any]] = {}
        for col in table.columns:
            stats = await _probe_column(registry, table.name, col, timeout_s)
            if stats:
                columns[col.name] = stats
        results[table.name] = {"row_count": row_count, "columns": columns}
    return results


def merge_into_stats(
    notes: dict[str, Any],
    profiled: dict[str, dict[str, Any]],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """把探测统计写入 schema_notes.yml 结构。

    Non-destructive by default:已有 stats 的列保持不动(overwrite=True 全量
    覆盖);表级 row_count 同理。返回新 dict,不修改调用方结构。
    """
    merged = copy.deepcopy(notes)
    by_name = {str(t["name"]): t for t in merged.get("tables", [])}
    for table_name, table_stats in profiled.items():
        table = by_name.get(table_name)
        if table is None:
            continue
        if overwrite or "row_count" not in table:
            if table_stats.get("row_count") is not None:
                table["row_count"] = table_stats["row_count"]
        col_by_name = {str(c["name"]): c for c in table.setdefault("columns", [])}
        for col_name, stats in (table_stats.get("columns") or {}).items():
            col = col_by_name.get(col_name)
            if col is None:
                continue
            if overwrite or not col.get("stats"):
                col["stats"] = {k: v for k, v in stats.items() if v is not None}
    return merged
