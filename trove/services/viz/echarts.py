"""ECharts option builder — turn the chart payload into a drop-in option dict.

The static API frontend renders this directly with ECharts; the payload
itself (``chart`` field) is the compact interchange format.
"""

from __future__ import annotations

from typing import Any


def build_echarts_option(chart: dict[str, Any] | None) -> dict[str, Any] | None:
    """Full ECharts ``option`` object from the chart payload (None → no chart)."""
    if not chart:
        return None
    chart_type = chart.get("type", "bar")
    categories = chart.get("categories", [])
    series = []
    for s in chart.get("series", []):
        series.append({"name": s.get("name", ""), "type": chart_type, "data": s.get("data", [])})
    option: dict[str, Any] = {
        "title": {"text": chart.get("title", "") or ""},
        "tooltip": {"trigger": "item" if chart_type == "pie" else "axis"},
        "legend": {"data": [s.get("name", "") for s in series]} if chart_type != "pie" else {},
        "series": series,
    }
    if chart_type == "pie":
        # single-measure pie: [{"name": cat, "value": v}, ...]
        dim_label = chart.get("dimension", "category")
        pie_data = [
            {"name": c, "value": v}
            for c, v in zip(categories, (chart.get("series") or [{}])[0].get("data", []))
        ]
        option["series"] = [{"name": dim_label, "type": "pie", "data": pie_data}]
        return option
    option["xAxis"] = {"type": "category", "data": categories}
    option["yAxis"] = {"type": "value"}
    return option