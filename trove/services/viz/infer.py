"""Deterministic chart-type inference from result columns + rows.

Zero LLM: the chart choice follows column dtype and cardinality rules so
tests and production behave identically. Numeric columns are measures;
everything else is a candidate dimension (time-like names/values steer
toward a line chart).

  - no measure      → chart_type "none" (nothing to plot)
  - time dimension  → line (multi-measure → multi-series line)
  - 1 measure, ≥2 ≤6 categories, proportional values → pie
  - otherwise       → bar (multi-series when >1 measure)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_TIME_NAME_RE = re.compile(
    r"date|time|year|month|day|week|日期|时间|年份|月份|日期|月度|季度|年份"
)
_TIME_VALUE_RE = re.compile(
    r"^\d{4}[-/]\d{1,2}(?:-\d{1,2})?$"
    r"|^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$"
)
_INT_RE = re.compile(r"^[-+]?\d+$")
_FLOAT_RE = re.compile(r"^[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?$")


def is_numeric_column(values: list[Any]) -> bool:
    """A column is numeric when ≥80% of non-null cells parse as numbers."""
    nums = 0
    total = 0
    for v in values:
        if v is None or v == "":
            continue
        total += 1
        s = str(v).strip()
        if _INT_RE.match(s) or _FLOAT_RE.match(s):
            nums += 1
    return total > 0 and nums / total >= 0.8


def _num(value: Any) -> float:
    s = str(value or 0).strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def is_time_dimension(name: str, values: list[Any]) -> bool:
    """Time-like dimension: by column name or by value shape."""
    if _TIME_NAME_RE.search(name.lower()):
        return True
    sampled = [str(v).strip() for v in values[:12] if v not in (None, "")]
    return bool(sampled) and sum(
        1 for s in sampled if _TIME_VALUE_RE.match(s)
    ) / len(sampled) >= 0.5


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


def infer_chart(columns: list[str], rows: list[list[Any]]) -> ChartSpec:
    """Chart decision from columns + rows (empty/default-safe)."""
    if not columns or rows is None:
        return ChartSpec()
    idx = {c: i for i, c in enumerate(columns)}
    numeric = [c for c in columns if is_numeric_column([row[idx[c]] for row in rows])]
    dims = [c for c in columns if c not in numeric]
    if not numeric or not dims:
        return ChartSpec()

    dimension = dims[0]
    measures = numeric[:4]  # cap series count; extra measures stay in the table
    cat_values = [row[idx[dimension]] for row in rows]
    is_time = is_time_dimension(dimension, cat_values)

    if is_time:
        return ChartSpec(chart_type="line", dimension=dimension, measures=measures)
    if (
        len(measures) == 1
        and 2 <= len({str(v) for v in cat_values}) <= 6
        and _is_proportional([row[idx[measures[0]]] for row in rows])
    ):
        return ChartSpec(chart_type="pie", dimension=dimension, measures=measures)
    return ChartSpec(chart_type="bar", dimension=dimension, measures=measures)


def build_chart(
    columns: list[str], rows: list[list[Any]], spec: ChartSpec, title: str = "",
) -> dict[str, Any] | None:
    """Assemble the ECharts-consumable chart payload; None when not chartable."""
    if not spec or spec.chart_type == "none" or not spec.dimension or not spec.measures:
        return None
    idx = {c: i for i, c in enumerate(columns)}
    dim_i = idx[spec.dimension]
    categories: list[str] = []
    seen: set[str] = set()
    for row in rows:
        label = str(row[dim_i]) if row[dim_i] is not None else ""
        if label in seen:
            continue
        seen.add(label)
        categories.append(label)
        if len(categories) >= 200:  # plotting guard
            break

    series = []
    for measure in spec.measures:
        m_i = idx[measure]
        data = []
        for row in rows[: len(categories)]:
            v = row[m_i] if m_i < len(row) else None
            data.append(_num(v))
        series.append({"name": measure, "data": data})

    return {
        "type": spec.chart_type,
        "title": (title or "").strip()[:60],
        "dimension": spec.dimension,
        "categories": categories,
        "series": series,
        "measures": spec.measures,
    }