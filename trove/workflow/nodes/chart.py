"""Chart node — deterministic chart inference from execution results.

Zero LLM: chart type/dimension/measures come from column dtype + row
cardinality rules (see trove/services/viz/infer.py). When a semantic layer
is available, its declared time fields are passed as hints so time
detection reuses the semantic model instead of only regex heuristics
(e.g. a ``period`` column of ``2024Q1`` codes declared as time). Empty-safe:
no columns, zero rows, or no numeric measure → pass-through (no chart).

Node shape: `async def chart(state: WorkflowState) -> dict` returns a
partial state update {"chart": {...} | cleared}.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trove.services.semantic_layer.compiler import _is_time_field
from trove.services.viz.infer import build_chart, infer_chart
from trove.workflow.state import WorkflowState


def _semantic_time_hints(semantic_layer: Any, matched_tables: list[str]) -> dict | None:
    """已匹配数据集里声明为时间的字段名 → {"time_columns": [...]}。

    取自语义模型(SemanticModel)的字段 is_time/语义角色/时态 datatype,
    以及命中度量的 agg_time_dimension 尾缀——让图表时间判定复用语义声明。
    解析失败 / 无语义层 / 无命中 → None(回退纯启发式)。
    """
    try:
        model = semantic_layer.model()
    except Exception:
        return None
    if model is None or not matched_tables:
        return None
    matched = {str(t).lower() for t in matched_tables}
    names: list[str] = []
    seen: set[str] = set()
    for d in model.datasets:
        if d.name.lower() not in matched:
            continue
        for f in d.fields:
            if not _is_time_field(f):
                continue
            tail = str(f.name).split(".")[-1].lower()
            if tail not in seen:
                seen.add(tail)
                names.append(f.name)
    for m in model.metrics:
        atd = m.agg_time_dimension
        if atd:
            tail = str(atd).split(".")[-1].lower()
            if tail not in seen:
                seen.add(tail)
                names.append(tail)
    return {"time_columns": names} if names else None


def make_chart(semantic_layer: Any = None) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    async def chart(state: WorkflowState) -> dict[str, Any]:
        if state.error or not state.columns or state.row_count == 0:
            # 降级/空结果 → 清掉陈旧图表(重跑修正轮可能换结果)
            return {"chart": None}
        hints = None
        if semantic_layer is not None:
            hints = _semantic_time_hints(semantic_layer, state.matched_tables)
        try:
            spec = infer_chart(state.columns, state.rows, hints)
            payload = build_chart(state.columns, state.rows, spec, state.question)
        except Exception:
            return {}  # 图表推断失败绝不阻断输出链路
        return {"chart": payload}

    return chart
