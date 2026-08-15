"""Consensus selection node — multi-candidate result agreement.

Executes the alternative candidate SQL and compares its result set with
the primary's. Agreement is a strong correctness signal (no LLM judge
involved); disagreement routes back to gen_sql through the shared
error_feedback channel with a concrete reason.

Order in the graph: execute_sql → select → validate → route.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from trove.services.datasource.registry import ConnectorRegistry
from trove.workflow.state import WorkflowState


def _normalize_rows(rows: list[list[Any]]) -> list[tuple[str, ...]]:
    """Set comparison: sorted, stringified rows (order/type insensitive)."""
    return sorted(tuple(str(v) for v in row) for row in rows)


def make_select_consensus(
    connectors: ConnectorRegistry | None = None,
    timeout_ms: int = 30000,
    max_retries: int = 10,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the consensus select node.

    Args:
        connectors: Registry used to execute the alternative candidate.
        timeout_ms: Timeout for the alternative execution.
        max_retries: Shared correction budget (same semantics as execute).
    """

    async def select(state: WorkflowState) -> dict[str, Any]:
        # Upstream failure / pending feedback / no candidates — pass through
        if state.error or state.error_feedback or not state.candidates:
            return {}
        if connectors is None:
            return {}

        alt_sql = state.candidates[0]
        try:
            alt_result = await asyncio.wait_for(
                connectors.execute(alt_sql), timeout=timeout_ms / 1000.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Alternative failed to run — keep the primary silently
            return {}

        if _normalize_rows(alt_result.rows) == _normalize_rows(state.rows):
            return {}  # consensus — high confidence, no correction needed

        if state.retry_count >= max_retries:
            # Budget exhausted: deliver the primary with a low-confidence
            # mark instead of degrading to an error.
            return {"consensus": False}
        feedback = (
            f"Candidate SQL variants returned different results "
            f"({state.row_count} vs {alt_result.row_count} rows) — "
            f"the query logic is unstable; verify and regenerate."
        )
        return {
            "error_feedback": feedback,
            "retry_count": state.retry_count + 1,
            "correction_history": [feedback],
            "consensus": False,
        }

    return select
