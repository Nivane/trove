"""Clarify node — ask the user instead of generating SQL when in doubt.

Deterministic trigger: the question matched no tables (and no term
hits brought tables in). Generating SQL with an empty schema context
is guessing — a clarifying question is cheaper and more honest.

The user's answer becomes the next turn; conversation history carries
the context into generation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.i18n import L, detect_language
from trove.workflow.state import WorkflowState

CLARIFY_NO_MATCH = (
    "你的问题没有匹配到任何表或业务术语。"
    "请补充具体的数据范围（表名、指标名或业务术语），例如「loan 表的贷款总额」。"
)


def make_clarify() -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the clarify node."""

    async def clarify(state: WorkflowState) -> dict[str, Any]:
        # Upstream failure — pass through
        if state.error:
            return {}
        # No tables matched → ask rather than guess
        if not state.matched_tables:
            lang = detect_language(state.question)
            return {"clarification_question": L(
                lang,
                CLARIFY_NO_MATCH,
                "Your question did not match any table or business term. "
                "Please specify the data scope (table name, metric, or term), "
                'e.g. "total loan amount in the loan table".',
            )}
        return {}

    return clarify
