"""Chart node — LLM-decided chart inference from execution results.

When ``config.chart_llm`` is enabled and an LLM is available, the node
forces a single ``plot_chart`` tool call: the LLM decides whether a chart
is worth rendering and which spec (chart type / dimension / measures) to
use, and the tool handler validates every column against the actual result
columns before assembling the ECharts payload. On any LLM/tool failure
(no LLM, timeouts, invalid output) it falls back to the deterministic
``infer_chart`` heuristic so a chart is still produced where possible.

Zero-LLM path (default): chart type/dimension/measures come from column
dtype + row cardinality rules (see trove/services/viz/infer.py). When a
semantic layer is available, its declared time fields are passed as hints
so time detection reuses the semantic model instead of only regex
heuristics (e.g. a ``period`` column of ``2024Q1`` codes declared as
time). Empty-safe: no columns, zero rows, or no numeric measure →
pass-through (no chart).

Node shape: `async def chart(state: WorkflowState) -> dict` returns a
partial state update {"chart": {...} | cleared}.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.prompts import render
from trove.prompts.skills import append_skill_block, render_skills
from trove.services.semantic_layer.compiler import _is_time_field
from trove.services.viz.infer import build_chart, infer_chart
from trove.services.viz.tool import CHART_TOOL_NAME, build_chart_registry
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

MAX_CHART_ROWS = 20  # 注入给 LLM 判定的数据行上限(截断避免超长)


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


def _rows_preview(state: WorkflowState) -> tuple[list[list], str]:
    """给 LLM 判定的行预览:返回 **(结构化行, 截断警示)**,前 N 行、查询顺序。

    两件东西分开交,是为了让隔离核按结构逐叶处理行(设计稿 §6-5):迁移前
    警示拼在行文本尾部,整块是一个字符串,一个坏单元格会连警示一起吃掉。
    模板负责把两者拼回原位,渲染文本逐字节不变
    (tests/prompts/test_rows_render_bytes_unchanged.py)。
    """
    rows = state.rows[:MAX_CHART_ROWS]
    if state.row_count <= MAX_CHART_ROWS:
        return rows, ""
    note = (
        f"\nNote: only the first {MAX_CHART_ROWS} of {state.row_count} rows "
        "shown, in query order (may be unsorted)."
        if state.lang != "zh" else
        f"\n注意：仅展示 {state.row_count} 行中的前 {MAX_CHART_ROWS} 行，"
        "为查询返回顺序（可能未排序）。"
    )
    return rows, note


async def _llm_chart(
    state: WorkflowState,
    llm: LLMGateway,
    config: AgentConfig,
    hints: dict | None,
    skills: Any | None = None,
) -> dict[str, Any] | None:
    """单次强制 plot_chart 工具调用:LLM 判定是否画图 + 图表规格。

    返回构建好的 chart payload;LLM 明确 chartable=false → None。任何异常
    向上抛,由调用方回退确定性推断。

    ``skills`` 由 ``make_chart`` 透传 —— system prompt 在这里组装,所以挂点
    也只能落在这里。本函数全仓只有 ``make_chart`` 一个调用方。
    """
    registry = build_chart_registry(
        state.columns, state.rows,
        hints=hints, title=state.question,
    )
    model = config.model_for_node("chart", state.complexity)
    rows, rows_note = _rows_preview(state)
    user_prompt = render(
        "chart/user",
        lang=state.lang,
        question=state.question,
        sql=state.sql,
        columns=state.columns,
        total_rows=state.row_count,
        rows=rows,
        rows_note=rows_note,
    )
    response = await llm.chat_full(
        model=model,
        messages=[
            # 原地改,不是插入:这是这条 system 消息的唯一来源。
            {"role": "system", "content": append_skill_block(
                render("chart/system", lang=state.lang),
                skills.render_skills("chart", **state.skill_ctx())
                if skills is not None
                else render_skills("chart", **state.skill_ctx()),
            )},
            {"role": "user", "content": user_prompt},
        ],
        tools=registry.defs(),
        tool_choice={"type": "function", "function": {"name": CHART_TOOL_NAME}},
        temperature=0.0,
        max_tokens=2000,
        metadata={
            "node": "chart",
            "session_id": state.session_id,
            "run_id": state.run_id,
            "question": state.question[:80],
        },
    )
    calls = (response or {}).get("tool_calls") or []
    if not calls:
        raise ValueError("LLM did not call plot_chart")
    arguments = calls[0].get("arguments") or "{}"
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as e:
            raise ValueError(f"plot_chart arguments not JSON: {e}") from e
    handler = registry.handlers()[CHART_TOOL_NAME]
    observation = await handler(arguments)
    if observation.startswith("ERROR:"):
        raise ValueError(observation)
    return registry.chart_payload


def make_chart(
    llm: LLMGateway | None = None,
    config: AgentConfig | None = None,
    semantic_layer: Any = None,
    skills: Any | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """``skills`` (optional ``SkillService``) 透传给 ``_llm_chart`` —— 图表
    选型口径(什么时候折线、什么时候条形)是组织口径,不是模型能力。"""
    async def chart(state: WorkflowState) -> dict[str, Any]:
        if state.error or not state.columns or state.row_count == 0:
            # 降级/空结果 → 清掉陈旧图表(重跑修正轮可能换结果)
            return {"chart": None}
        hints = None
        if semantic_layer is not None:
            hints = _semantic_time_hints(semantic_layer, state.matched_tables)
        # LLM 判定路径(配置开启 + 有 LLM):失败一律回退确定性推断
        if llm is not None and config is not None and config.chart_llm:
            try:
                payload = await _llm_chart(state, llm, config, hints, skills=skills)
                return {"chart": payload, "chart_source": "llm"}
            except Exception as e:
                logger.warning("LLM chart decision failed (%s); falling back", e)
        try:
            spec = infer_chart(state.columns, state.rows, hints)
            payload = build_chart(state.columns, state.rows, spec, state.question)
        except Exception:
            return {}  # 图表推断失败绝不阻断输出链路
        return {"chart": payload, "chart_source": "deterministic"}

    return chart
