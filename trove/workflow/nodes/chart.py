"""Chart node — deterministic chart inference from execution results.

Zero LLM: chart type/dimension/measures come from column dtype + row
cardinality rules (see trove/services/viz/infer.py). Empty-safe: no
columns, zero rows, or no numeric measure → pass-through (no chart).

Node shape: `async def chart(state: WorkflowState) -> dict` returns a
partial state update {"chart": {...} | cleared}.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trove.services.viz.infer import build_chart, infer_chart
from trove.workflow.state import WorkflowState


def make_chart() -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    async def chart(state: WorkflowState) -> dict[str, Any]:
        if state.error or not state.columns or state.row_count == 0:
            # 降级/空结果 → 清掉陈旧图表(重跑修正轮可能换结果)
            return {"chart": None}
        try:
            spec = infer_chart(state.columns, state.rows)
            payload = build_chart(state.columns, state.rows, spec, state.question)
        except Exception:
            return {}  # 图表推断失败绝不阻断输出链路
        return {"chart": payload}

    return chart