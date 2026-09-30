"""Conclusion node — LLM composes a one-sentence direct answer to the question.

Runs after execution + reflection accept the result (and after insights/chart),
before output. Uses the LLM to produce a short, data-grounded conclusion that is
rendered at the top of the answer (conclusion-first layout), so business users
see the answer before any SQL/table details.

Node shape: `make_conclusion(llm, config) -> async def conclusion(state) -> dict`
returns a partial state update. Passes through when there are no results or the
feature is disabled in config; failures degrade silently.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.prompts import render
from trove.prompts.skills import append_skill_block, render_skills
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

MAX_CONCLUSION_ROWS = 20  # 注入给 LLM 的最多数据行(截断避免超长)


def make_conclusion(
    llm: LLMGateway,
    config: AgentConfig,
    skills: Any | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the conclusion node bound to an LLM gateway.

    ``skills`` (optional ``SkillService``): confirmed org methodology
    skills matching this node are appended to the system prompt —
    结论写法(先结论后证据、标注口径与样本量)是组织口径,不是模型能力。
    """

    async def conclusion(state: WorkflowState) -> dict[str, Any]:
        if state.error or not state.sql or state.row_count < 0:
            return {}
        if not config.conclusion:
            return {}
        # 空结果表直接回答没有意义;跳过。
        if not state.rows:
            return {}

        # 交给模板的是**结构化行**,不是拼好的文本:回喂前的隔离核按结构逐叶
        # 替换,一个坏单元格不会吃掉整段预览(连带把 row_count / columns 的
        # 判断依据一起带走)。拼接由模板承担,渲染文本逐字节不变
        # (tests/prompts/test_rows_render_bytes_unchanged.py)。
        rows = state.rows[:MAX_CONCLUSION_ROWS]
        # 预览截断警示:仅展示前 N 行且为查询顺序(可能未排序)——避免 LLM
        # 从部分预览推断全局极值(前 20 行里"最高"未必真是全局最高)。
        rows_note = ""
        if state.row_count > MAX_CONCLUSION_ROWS:
            rows_note = (
                "Note: only the first "
                f"{MAX_CONCLUSION_ROWS} of {state.row_count} rows are shown, in "
                "query order (which may be unsorted) — do NOT infer a global "
                "max/min/extreme from this preview unless the SQL orders by it."
                if state.lang != "zh" else
                f"注意：以下仅展示 {state.row_count} 行中的前 {MAX_CONCLUSION_ROWS} 行，"
                "为查询返回顺序（可能未排序）——除非 SQL 已按其排序，不要仅凭预览"
                "推断全局最大/最小值或极值。"
            )
        model = config.model_for_node("conclusion", state.complexity)
        try:
            start = time.monotonic()
            user_prompt = render(
                "conclusion/user",
                lang=state.lang,
                question=state.question,
                time_context=state.time_context,
                sql=state.sql,
                columns=state.columns,
                total_rows=state.row_count,
                rows=rows,
                rows_note=rows_note,
            )
            response = await llm.chat(
                model=model,
                messages=[
                    # 原地改,不是插入:这是这条 system 消息的唯一来源。
                    {"role": "system", "content": append_skill_block(
                        render("conclusion/system", lang=state.lang),
                        skills.render_skills("conclusion", **state.skill_ctx())
                        if skills is not None
                        else render_skills("conclusion", **state.skill_ctx()),
                    )},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=16000,
                metadata={
                    "node": "conclusion",
                    "session_id": state.session_id,
                    "run_id": state.run_id,
                    "question": state.question[:80],
                },
            )
            text = (response or "").strip()
            if not text:
                return {"conclusion": ""}
            return {
                "conclusion": text,
                "llm": {
                    "model": model,
                    "elapsed_ms": int((time.monotonic() - start) * 1000),
                    "input_preview": user_prompt[:200],
                    "output_preview": (response or "")[:200],
                },
            }
        except Exception as e:
            logger.warning("Conclusion generation failed (%s); skipping", e)
            return {"conclusion": ""}

    return conclusion
