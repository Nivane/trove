"""Chart tool — lets the LLM decide whether to chart and with what spec.

The deterministic ``infer_chart`` heuristic (column dtype + cardinality
rules) is good, but the dimension/measure split it infers can miss the
semantically right view for a question. This module registers a
``plot_chart`` tool (via ``build_chart_registry``) whose parameters carry
the LLM's decision — chartable / chart_type / dimension / measures — and
whose handler validates every field against the actual result columns
before assembling the ECharts payload with ``build_chart``.

Contract (shared by the handler and the chart node):
  - ``chartable=false``  → no chart (explicit, respected)
  - chart_type ∈ line/bar/pie; dimension and measures must exist in the
    result columns and measures must be non-empty and numeric-parsable.
  - any invalid input → (None, reason): the caller falls back to the
    deterministic ``infer_chart`` path.

The handler stores the built payload on ``registry.chart_payload`` so the
node can read it after a single forced tool call (mirrors the
``registry.check_hits`` attribution pattern in gen_sql).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from trove.services.viz.infer import (
    MAX_MEASURES,
    ChartSpec,
    build_chart,
    is_numeric_column,
    is_time_dimension,
)

CHART_TOOL_NAME = "plot_chart"

_CHART_TYPES = ("line", "bar", "pie")


def chart_tool_def() -> dict[str, Any]:
    """Tool definition (JSON schema) describing the plot_chart contract."""
    return {
        "type": "object",
        "properties": {
            "chartable": {
                "type": "boolean",
                "description": (
                    "Whether a chart is worth rendering for this question. "
                    "false when the result is a single scalar value, has no "
                    "numeric measure, or a chart would add nothing over the table."
                ),
            },
            "chart_type": {
                "type": "string",
                "enum": list(_CHART_TYPES),
                "description": (
                    "line for a time/trend dimension; pie for shares of a whole; "
                    "bar otherwise."
                ),
            },
            "dimension": {
                "type": "string",
                "description": "The category/time column to plot along (must be one of the result columns).",
            },
            "measures": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Numeric measure columns to plot (must be result columns).",
            },
        },
        "required": ["chartable"],
    }


def chart_from_decision(
    columns: list[str],
    rows: list[list[Any]],
    decision: dict[str, Any],
    *,
    hints: dict | None = None,
    title: str = "",
) -> tuple[dict[str, Any] | None, str]:
    """Validate an LLM chart decision and build the chart payload.

    Args:
        columns: Result column names.
        rows: Result rows.
        decision: LLM arguments (chartable / chart_type / dimension / measures).
        hints: Optional semantic time-column hints (see infer_chart).
        title: Chart title (question text).

    Returns:
        (payload, "") on success; (None, reason) when not chartable or when
        the decision is invalid (caller falls back to deterministic infer).
    """
    if not isinstance(decision, dict):
        return None, "decision must be an object"
    if not decision.get("chartable", False):
        return None, ""
    chart_type = decision.get("chart_type") or ""
    if chart_type not in _CHART_TYPES:
        return None, f"chart_type must be one of {', '.join(_CHART_TYPES)}"
    dimension = decision.get("dimension") or ""
    if dimension not in columns:
        return None, f"dimension {dimension!r} is not a result column"
    measures = decision.get("measures") or []
    if not isinstance(measures, list) or not measures:
        return None, "measures must be a non-empty array"
    measures = [m for m in measures if m in columns]
    if not measures:
        return None, "no measures match the result columns"
    idx = {c: i for i, c in enumerate(columns)}
    bad = [m for m in measures if not is_numeric_column([row[idx[m]] for row in rows])]
    if bad:
        return None, f"measures are not numeric: {', '.join(bad)}"
    all_measures = list(measures)
    measures = measures[:MAX_MEASURES]
    is_time = is_time_dimension(dimension, [row[idx[dimension]] for row in rows], hints)
    spec = ChartSpec(
        chart_type=chart_type,
        dimension=dimension,
        measures=measures,
        is_time=is_time,
        truncated=len(all_measures) > MAX_MEASURES,
    )
    payload = build_chart(columns, rows, spec, title)
    if payload is None:
        return None, "chart could not be built"
    return payload, ""


def make_chart_tool(
    columns: list[str],
    rows: list[list[Any]],
    *,
    hints: dict | None = None,
    title: str = "",
) -> tuple[Callable[[dict[str, Any]], Awaitable[str]], dict[str, Any]]:
    """Build (handler, def) for the plot_chart tool bound to a result.

    The handler validates the decision and returns a JSON-serialized payload
    as the observation; a non-chartable or invalid decision returns a
    reason string prefixed with ``ERROR:`` so callers can fall back.
    """
    async def handler(arguments: dict[str, Any]) -> str:
        payload, reason = chart_from_decision(
            columns, rows, arguments, hints=hints, title=title)
        if payload is None and reason:
            return f"ERROR: {reason}"
        if payload is None:
            return "NO_CHART"
        return "OK " + json.dumps(payload, ensure_ascii=False)

    return handler, chart_tool_def()


def build_chart_registry(
    columns: list[str],
    rows: list[list[Any]],
    *,
    hints: dict | None = None,
    title: str = "",
    roles: list[str] | None = None,
) -> Any:
    """Register the plot_chart tool on a ToolRegistry for a result.

    The registered handler sets ``registry.chart_payload`` (None when not
    chartable; the built payload dict otherwise) so the caller can read the
    result after a single forced tool call.
    """
    from trove.llm.agent_loop import ToolRegistry

    registry = ToolRegistry(finish=False, allowed_roles=roles)
    registry.chart_payload = None

    async def _plot(arguments: dict[str, Any]) -> str:
        payload, reason = chart_from_decision(
            columns, rows, arguments, hints=hints, title=title)
        if payload is None and reason:
            return f"ERROR: {reason}"
        registry.chart_payload = payload
        return "OK " + json.dumps(payload, ensure_ascii=False) if payload else "NO_CHART"

    registry.register(
        CHART_TOOL_NAME,
        _plot,
        description=(
            "Decide whether a chart is worth rendering for the query result "
            "and submit its spec. Set chartable=false for scalar/single-value "
            "results. Otherwise choose chart_type (line for time trends, pie "
            "for shares, bar otherwise), one dimension column and one or more "
            "numeric measure columns — names must be taken from the result "
            "columns exactly."
        ),
        parameters=chart_tool_def(),
        parallel=False,
    )
    return registry
