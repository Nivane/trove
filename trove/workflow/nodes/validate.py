"""Deterministic validation node — rule checks between execute and reflect.

Rule failures use the same error_feedback correction channel as
execution errors (shared retry budget): the regenerated SQL gets the
concrete rule reason in its prompt. This is the code-side counterpart
to the LLM reflect judge — what can be checked deterministically
should not be left to the model.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.i18n import L
from trove.workflow.rules import verify as run_rules
from trove.workflow.state import WorkflowState


def make_validate_rules(
    max_retries: int = 10,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the validate node.

    Args:
        max_retries: Shared correction budget — rule failures feed back
            to gen_sql while retry_count < max_retries; once exhausted,
            failures degrade gracefully via state.error.

    The verify_step assertion layer reports structured hits
    (rule name + reason) via state.validation_hits for eval attribution.
    """

    async def validate(state: WorkflowState) -> dict[str, Any]:
        # Upstream failure / pending execution feedback — pass through
        if state.error or state.error_feedback:
            return {}

        reason, hits = run_rules(
            state.question, state.sql, state.columns, state.rows, state.row_count,
            lang=state.lang,
        )
        if reason is None:
            return {}

        if state.retry_count >= max_retries:
            return {"error": reason}
        feedback = L(
            state.lang,
            f"校验规则: {reason}",
            f"Validation rule: {reason}",
        )
        return {
            "error_feedback": feedback,
            "retry_count": state.retry_count + 1,
            "correction_history": [feedback],
            "validation_hits": hits,
        }

    return validate
