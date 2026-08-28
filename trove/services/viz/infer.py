"""Deterministic chart-type inference from result columns + rows.

Zero LLM: the chart choice follows column dtype and cardinality rules so
tests and production behave identically. Numeric columns (including
percentage strings) are measures; everything else is a candidate dimension
(time-like names/values steer toward a line chart).

  - no measure      → chart_type "none" (nothing to plot)
  - time dimension  → line (multi-measure → multi-series line)
  - 1 measure, ≥2 ≤6 categories, proportional values → pie
  - otherwise       → bar (multi-series when >1 measure)

Corrections over the previous heuristic version:

  - duplicate category values are aggregated (summed) so categories and
    series data always stay aligned (a repeated dimension value used to
    shift every subsequent bar/line point);
  - a time-like dimension is sorted chronologically before plotting (an
    unordered time series no longer renders as a zigzag line);
  - quarter codes (``Q1`` / ``2024Q1``), compact ``YYYYMM`` and epoch
    timestamps are recognised as time so trend queries still chart;
  - percentage strings (``78%``) are treated as numeric measures;
  - ``infer_chart`` accepts optional semantic-layer ``hints`` (declared
    time fields) so the chart can reuse the semantic model instead of only
    guessing from names/values;
  - capping (200 categories / 4 measures) is surfaced via ``truncated``
    instead of being silent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 名称信号:时间/日期类列名(去重后)。``period``/``quarter`` 等分析列
# 名常因值形态特殊(2024Q1/Q1)而漏判,名称命中即可兜底。
_TIME_NAME_RE = re.compile(
    r"date|time|year|month|day|week|quarter|period|日期|时间|年份|月份|"
    r"季度|月度|期间|周",
    re.I,
)
# 值信号:能解析成时间的常见形态(含季度码 / 紧凑年月 / epoch 秒)。
_YMD_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
_YM_RE = re.compile(r"^\d{4}[-/]\d{1,2}$")
_MMYYYY_RE = re.compile(r"^\d{1,2}[-/]\d{4}$")
_YYMM_RE = re.compile(r"^\d{4}\d{2}$")
_YQ_RE = re.compile(r"^\d{4}[-/]?[Qq][1-4]$")
_QM_RE = re.compile(r"^[Qq][1-4]$")
_SLASH_DATE_RE = re.compile(r"^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$")
_YEAR_RE = re.compile(r"^\d{4}$")
_EPOCH_RE = re.compile(r"^\d{9,11}$")
_INT_RE = re.compile(r"^[-+]?\d+$")
_FLOAT_RE = re.compile(r"^[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?$")
_PERCENT_RE = re.compile(r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)\s*%$")

MAX_CATEGORIES = 200
MAX_MEASURES = 4

# 按顺序匹配,首个命中的时间形态给出排序键:
#   (year, month, day, epoch) —— 季度/年/月/日都可比较。
_QUARTER_MS = {1: 1, 2: 4, 3: 7, 4: 10}  # 季度起始月


def _time_sort_key(value: Any) -> tuple[int, int, int, int] | None:
    """把一个时间形态的值解析成可比较键;解析失败 → None(调用方放弃排序)。"""
    s = str(value or "").strip()
    if not s:
        return None
    m = _YQ_RE.match(s)
    if m:
        y, q = int(m.group(0)[:4]), int(s[-1])
        return (y, _QUARTER_MS.get(q, 1), 0, 0)
    if _QM_RE.match(s):
        return (0, _QUARTER_MS.get(int(s[-1]), 1), 0, 0)
    m = _YMD_RE.match(s)
    if m:
        y, mo, d = m.group(0).split("-") if "-" in s else m.group(0).split("/")
        return (int(y), int(mo), int(d), 0)
    m = _YM_RE.match(s)
    if m:
        y, mo = (m.group(0).split("-") if "-" in s else m.group(0).split("/"))
        return (int(y), int(mo), 0, 0)
    m = _MMYYYY_RE.match(s)
    if m:
        mo, y = (m.group(0).split("/") if "/" in s else m.group(0).split("-"))
        return (int(y), int(mo), 0, 0)
    m = _YYMM_RE.match(s)
    if m:
        return (int(s[:4]), int(s[4:]), 0, 0)
    m = _SLASH_DATE_RE.match(s)
    if m:
        a, b, c = (m.group(0).split("/") if "/" in s else m.group(0).split("-"))
        y = int(c) if len(c) == 4 else 2000 + int(c)
        return (y, int(a), int(b), 0)
    if _YEAR_RE.match(s):
        y = int(s)
        if 1000 <= y <= 2100:
            return (y, 0, 0, 0)
        return None
    if _EPOCH_RE.match(s):
        return (0, 0, 0, int(s))  # epoch 秒单调 → 可直接排序
    return None


def is_time_value(value: Any) -> bool:
    """单个值是否解析成时间形态(年/月/日/季度/紧凑年月/epoch)。"""
    return _time_sort_key(value) is not None


def _values_time_like(values: list[Any]) -> bool:
    """Bulk of the cells look like dates/years/quarters (``2024-01``, ``2020``,
    ``Q1``, ``2024Q1``, epoch seconds...).

    A year column of 4-digit integers parses as numeric; it must stay a
    dimension, otherwise year-over-year queries end up with no dimension
    and no chart.
    """
    sampled = [str(v).strip() for v in values[:12] if v not in (None, "")]
    if not sampled:
        return False
    hits = sum(1 for s in sampled if is_time_value(s))
    return hits / len(sampled) >= 0.5


def is_numeric_column(values: list[Any]) -> bool:
    """A column is numeric when ≥80% of non-null cells parse as numbers
    (percentage strings like ``78%`` count as numeric)."""
    nums = 0
    total = 0
    for v in values:
        if v is None or v == "":
            continue
        total += 1
        s = str(v).strip()
        if _INT_RE.match(s) or _FLOAT_RE.match(s) or _PERCENT_RE.match(s):
            nums += 1
    return total > 0 and nums / total >= 0.8


def _num(value: Any) -> float:
    """Robust numeric parse: strips ``%``, thousands separators and currency
    symbols; unparseable → 0.0 (kept for the table/chart to never break)."""
    if value is None:
        return 0.0
    s = str(value).strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    s = s.replace(",", "").replace("，", "")
    s = s.lstrip("¥$€£￥")
    try:
        return float(s)
    except ValueError:
        return 0.0


def is_time_dimension(name: str, values: list[Any], hints: dict | None = None) -> bool:
    """Time-like dimension: by column name, by value shape, or by semantic hint."""
    if _TIME_NAME_RE.search(name.lower()):
        return True
    if _values_time_like(values):
        return True
    if hints:
        time_hints = {str(h).lower() for h in (hints.get("time_columns") or [])}
        tail = name.split(".")[-1].lower()
        if name.lower() in time_hints or tail in time_hints:
            return True
    return False


def _is_proportional(values: list[Any]) -> bool:
    """All non-null values in (0, 1] or percentage strings → slice-able."""
    for v in values:
        if v is None or v == "":
            continue
        s = str(v).strip()
        if s.endswith("%"):
            continue
        try:
            n = float(s)
        except ValueError:
            return False
        if not (0 < n <= 1):
            return False
    return True


@dataclass
class ChartSpec:
    """Structural decision: which columns play which chart role."""

    chart_type: str = "none"  # line | bar | pie | none
    dimension: str = ""
    measures: list[str] = field(default_factory=list)
    is_time: bool = False
    truncated: bool = False


def infer_chart(
    columns: list[str],
    rows: list[list[Any]],
    hints: dict | None = None,
) -> ChartSpec:
    """Chart decision from columns + rows (empty/default-safe).

    Dimension / measure split follows column dtype AND shape: time-like
    columns (name, values — e.g. a bare ``year`` of 4-digit integers or a
    ``period`` of ``2024Q1`` codes — or a semantic-layer hint) are always
    dimensions, so year-over-year / trend queries still chart even when the
    time column parses as numbers.

    ``hints``: optional dict with ``time_columns`` — declared time field
    names from the semantic model (see trove/services/semantic_layer/), so
    time detection no longer depends solely on regex heuristics.
    """
    if not columns or rows is None:
        return ChartSpec()
    idx = {c: i for i, c in enumerate(columns)}
    values = {c: [row[idx[c]] for row in rows] for c in columns}
    dims: list[str] = []
    measures: list[str] = []
    for c in columns:
        cv = values[c]
        if is_time_dimension(c, cv, hints):
            dims.append(c)
        elif is_numeric_column(cv):
            measures.append(c)
        else:
            dims.append(c)
    if not measures or not dims:
        return ChartSpec()

    dimension = dims[0]
    all_measures = list(measures)
    measures = measures[:MAX_MEASURES]  # cap series count; extra stay in the table
    truncated = len(all_measures) > MAX_MEASURES
    cat_values = values[dimension]
    is_time = is_time_dimension(dimension, cat_values, hints)

    if is_time:
        return ChartSpec(
            chart_type="line", dimension=dimension, measures=measures,
            is_time=True, truncated=truncated,
        )
    if (
        len(measures) == 1
        and 2 <= len({str(v) for v in cat_values}) <= 6
        and _is_proportional(values[measures[0]])
    ):
        return ChartSpec(
            chart_type="pie", dimension=dimension, measures=measures,
            is_time=False, truncated=truncated,
        )
    return ChartSpec(
        chart_type="bar", dimension=dimension, measures=measures,
        is_time=False, truncated=truncated,
    )


def build_chart(
    columns: list[str], rows: list[list[Any]], spec: ChartSpec, title: str = "",
) -> dict[str, Any] | None:
    """Assemble the ECharts-consumable chart payload; None when not chartable.

    Duplicate category values are aggregated (summed) so categories and
    series data stay aligned; a time-like dimension is sorted
    chronologically. Capping is surfaced via ``truncated``.
    """
    if not spec or spec.chart_type == "none" or not spec.dimension or not spec.measures:
        return None
    idx = {c: i for i, c in enumerate(columns)}
    dim_i = idx[spec.dimension]

    # Aggregate duplicate categories: each measure summed per category so
    # the deduped categories and the series data always align.
    buckets: dict[str, list[float]] = {}
    for row in rows:
        label = str(row[dim_i]) if row[dim_i] is not None else ""
        vals = []
        for measure in spec.measures:
            m_i = idx[measure]
            vals.append(_num(row[m_i]) if m_i < len(row) else 0.0)
        if label in buckets:
            buckets[label] = [a + b for a, b in zip(buckets[label], vals)]
        else:
            buckets[label] = vals

    categories = list(buckets.keys())
    truncated = spec.truncated
    if len(categories) > MAX_CATEGORIES:
        categories = categories[:MAX_CATEGORIES]
        truncated = True

    # Chronological sort for time dimensions (skipped when any category
    # fails to parse — preserve original row order).
    if spec.is_time and len(categories) > 1:
        keys = [_time_sort_key(c) for c in categories]
        if all(k is not None for k in keys):
            order = sorted(range(len(categories)), key=lambda i: keys[i])
            categories = [categories[i] for i in order]
            buckets = {c: buckets[c] for c in categories}

    series = [
        {"name": measure, "data": [buckets[c][i] for c in categories]}
        for i, measure in enumerate(spec.measures)
    ]

    return {
        "type": spec.chart_type,
        "title": (title or "").strip()[:60],
        "dimension": spec.dimension,
        "categories": categories,
        "series": series,
        "measures": spec.measures,
        "truncated": truncated,
    }
