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
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the execute_sql node bound to a connector registry.

    Args:
        connectors: Registry used to run SQL (None → error update).
        timeout_ms: Query timeout in milliseconds.

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
            return {"error": f"Query timed out after {timeout_ms}ms"}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return {"error": str(e)}

        return {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "execution_time_ms": result.execution_time_ms,
        }

    return execute_sql
