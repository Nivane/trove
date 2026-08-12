"""ExecuteSQL node — runs generated SQL against the datasource.

Checks cancellation before executing and supports
interruption of long-running queries.
"""

from __future__ import annotations

import asyncio

from trove.core.types import NodeStatus, QueryResult, WorkflowContext
from trove.core.errors import SQLExecutionError, CancelledError
from trove.core.logging import get_logger
from trove.workflow.node import Node, NodeResult
from trove.workflow.node_type import NodeType

logger = get_logger(__name__)


class ExecuteSQLNode(Node):
    """Execute a SQL query against the active datasource.

    Requires gen_sql to have run first (reads sql from context).
    Supports cancellation via ctx.cancellation_event.
    """

    node_type = NodeType.EXECUTE_SQL

    def __init__(self, name: str = "execute_sql", timeout_ms: int = 30000):
        super().__init__(name)
        self.timeout_ms = timeout_ms

    async def execute(self, ctx: WorkflowContext) -> NodeResult:
        """Execute the SQL from the gen_sql node result.

        Args:
            ctx: Workflow context (reads sql from _node_data["gen_sql"]).

        Returns:
            NodeResult with query result data.
        """
        # Get SQL from upstream gen_sql node
        sql = ""
        if hasattr(ctx, '_node_data'):
            gen_data = ctx._node_data.get("gen_sql", {})  # type: ignore[attr-defined]
            sql = gen_data.get("sql", "")

        if not sql:
            return NodeResult(
                node_name=self.name,
                status=NodeStatus.ERROR,
                error=SQLExecutionError(
                    message="No SQL to execute — gen_sql node must run first",
                ),
            )

        # Check for cancellation
        if ctx.cancellation_event.is_set():
            raise CancelledError("Execution cancelled before start")

        # Get the connector registry from config
        registry = getattr(ctx.config, '_connector_registry', None)
        if registry is None:
            return NodeResult(
                node_name=self.name,
                status=NodeStatus.ERROR,
                error=SQLExecutionError(
                    message="No datasource registry available",
                    sql=sql,
                ),
            )

        # Execute with timeout
        try:
            result: QueryResult = await asyncio.wait_for(
                registry.execute(sql),
                timeout=self.timeout_ms / 1000.0,
            )

            # Check for cancellation during execution
            if ctx.cancellation_event.is_set():
                return NodeResult(
                    node_name=self.name,
                    status=NodeStatus.ERROR,
                    error=CancelledError("Execution cancelled during query"),
                    data={"sql": sql, "partial_result": result},
                )

            return NodeResult(
                node_name=self.name,
                status=NodeStatus.SUCCESS,
                data={
                    "sql": sql,
                    "columns": result.columns,
                    "rows": result.rows,
                    "row_count": result.row_count,
                    "execution_time_ms": result.execution_time_ms,
                },
            )

        except asyncio.TimeoutError:
            return NodeResult(
                node_name=self.name,
                status=NodeStatus.ERROR,
                error=SQLExecutionError(
                    message=f"Query timed out after {self.timeout_ms}ms",
                    sql=sql,
                ),
                data={"sql": sql},
            )
        except CancelledError:
            raise
        except Exception as e:
            return NodeResult(
                node_name=self.name,
                status=NodeStatus.ERROR,
                error=SQLExecutionError(
                    message=str(e),
                    sql=sql,
                    db_error=str(e),
                ),
                data={"sql": sql},
            )
