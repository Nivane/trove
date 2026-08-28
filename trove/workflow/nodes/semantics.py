"""Semantics node — explains the generated SQL in plain language.

Runs after gen_sql (before HITL / execution). Uses the LLM to translate
the SQL into a natural-language account of what it does and what its
result answers, so a human can review intent before approving execution.

Node shape: `make_semantics(llm, config) -> async def semantics(state) -> dict`
returns a partial state update. Passes through when SQL is absent, an
upstream error is set, or semantic explanation is disabled in config.
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


def make_semantics(
    llm: LLMGateway,
    config: AgentConfig,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the semantics node bound to an LLM gateway.

    Explains :attr:`state.sql` in plain language when ``config.explain_semantics``
    is enabled. Failures degrade silently (semantics left empty) rather than
    blocking the pipeline.
    """

    async def semantics(state: WorkflowState) -> dict[str, Any]:
        if state.error or not state.sql:
            return {}
        if not config.explain_semantics:
            return {}
        # 修正轮跳过:semantics 的唯一消费场景是 HITL 执行前确认
        # (hitl.py in_correction 直接放行,不暂停)——修正轮再生成解释
        # 是纯浪费,每次修正轮白烧一次 LLM 调用。
        in_correction = bool(
            state.error_feedback or state.error_analysis or state.reason
        )
        if in_correction:
            return {}

        model = config.model_for_node("semantics", state.complexity)
        try:
            start = time.monotonic()
            user_prompt = render(
                "semantics/user",
                lang=state.lang,
                question=state.question,
                time_context=state.time_context,
                sql=state.sql,
            )
            response = await llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": render("semantics/system", lang=state.lang)},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=16000,
                metadata={
                    "node": "semantics",
                    "session_id": state.session_id,
                    "run_id": state.run_id,
                    "question": state.question[:80],
                },
            )
            return {
                "semantics": (response or "").strip(),
                "llm": {
                    "model": model,
                    "elapsed_ms": int((time.monotonic() - start) * 1000),
                    "input_preview": user_prompt[:200],
                    "output_preview": (response or "")[:200],
                },
            }
        except Exception as e:
            logger.warning("Semantics explanation failed (%s); leaving empty", e)
            return {"semantics": ""}

    return semantics
