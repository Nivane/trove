"""Insights node — LLM generates data-grounded insights from query results.

Runs after execution + reflection accept the result, before output. Uses
the LLM to produce concise, factual insights from the returned rows.

Node shape: `make_insights(llm, config) -> async def insights(state) -> dict`
returns a partial state update. Passes through when there are no results
or insights are disabled in config; failures degrade silently.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.prompts import render
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

MAX_INSIGHT_ROWS = 20  # 注入给 LLM 的最多数据行(截断避免超长)


def make_insights(
    llm: LLMGateway,
    config: AgentConfig,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the insights node bound to an LLM gateway."""

    async def insights(state: WorkflowState) -> dict[str, Any]:
        if state.error or not state.sql or state.row_count < 0:
            return {}
        if not config.insights:
            return {}
        # 空结果表也生成洞察没有意义;直接跳过。
        if not state.rows:
            return {}

        rows_text = "\n".join(
            " | ".join(str(cell) for cell in row)
            for row in state.rows[:MAX_INSIGHT_ROWS]
        )
        model = config.model_for(state.complexity)
        try:
            start = time.monotonic()
            user_prompt = render(
                "insights/user",
                lang=state.lang,
                question=state.question,
                time_context=state.time_context,
                sql=state.sql,
                columns=state.columns,
                total_rows=state.row_count,
                rows=rows_text,
            )
            response = await llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": render("insights/system", lang=state.lang)},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=16000,
                metadata={
                    "node": "insights",
                    "session_id": state.session_id,
                    "run_id": state.run_id,
                    "question": state.question[:80],
                },
            )
            text = (response or "").strip()
            lines = [
                line.strip().lstrip("-•*").strip()
                for line in text.splitlines()
                if line.strip()
            ]
            return {
                "insights": lines[:6],
                "llm": {
                    "model": model,
                    "elapsed_ms": int((time.monotonic() - start) * 1000),
                    "input_preview": user_prompt[:200],
                    "output_preview": (response or "")[:200],
                },
            }
        except Exception as e:
            logger.warning("Insights generation failed (%s); skipping", e)
            return {"insights": []}

    return insights
