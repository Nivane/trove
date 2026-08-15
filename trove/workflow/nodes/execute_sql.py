"""ExecuteSQL node — runs generated SQL against the datasource.

Cancellation is handled by asyncio task cancellation (CancelledError
propagates through the graph); no explicit cancellation-event checks.

Node shape: `async def execute_sql(state: WorkflowState) -> dict`
returns a partial state update.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.logging import get_logger
from trove.services.datasource.registry import ConnectorRegistry
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)


def make_execute_sql(
    connectors: ConnectorRegistry | None = None,
    timeout_ms: int = 30000,
    max_retries: int = 2,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the execute_sql node bound to a connector registry.

    Args:
        connectors: Registry used to run SQL (None → error update).
        timeout_ms: Query timeout in milliseconds.
        max_retries: Shared correction budget — execution failures feed
            back to gen_sql for regeneration while retry_count < max_retries;
            once exhausted, failures degrade gracefully via state.error.

    Returns:
        Async node function taking WorkflowState and returning a partial update.
    """

    async def execute_sql(state: WorkflowState) -> dict[str, Any]:
        # Upstream node failed — pass through without running
        if state.error:
            return {}

        if not state.sql:
            return {"error": "No SQL to execute — SQL generation did not produce a query."}

        if connectors is None:
            return {"error": "No datasource registry available."}

        try:
            result = await asyncio.wait_for(
                connectors.execute(state.sql),
                timeout=timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            return _execution_failure(
                state, f"Query timed out after {timeout_ms}ms", max_retries,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return _execution_failure(state, str(e), max_retries)

        return {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "execution_time_ms": result.execution_time_ms,
            "error_feedback": "",  # success clears previous feedback
        }

    return execute_sql


def _execution_failure(
    state: WorkflowState, message: str, max_retries: int,
) -> dict[str, Any]:
    """Feed the error back to gen_sql, or degrade when the budget is spent."""
    if state.retry_count >= max_retries:
        return {"error": message}
    return {
        "error_feedback": message,
        "retry_count": state.retry_count + 1,
    }
