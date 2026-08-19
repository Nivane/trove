"""SQL executor — thin proxy over ConnectorRegistry.

Adds permission checks and cancellation support
around the raw database execution.
"""

from __future__ import annotations

import asyncio
from enum import Enum

from trove.core.types import QueryResult
from trove.core.errors import SQLExecutionError, CancelledError
from trove.core.logging import get_logger
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.sql.validator import SQLValidator

logger = get_logger(__name__)


class PermissionLevel(Enum):
    """SQL execution permission levels."""
    NORMAL = "normal"        # Confirm all operations
    AUTO = "auto"            # Read ops auto-approved, write ops blocked
    DANGEROUS = "dangerous"  # All auto-approved


class SQLExecutor:
    """Proxy that adds safety and cancellation around raw SQL execution."""

    def __init__(
        self,
        registry: ConnectorRegistry,
        permission_level: PermissionLevel = PermissionLevel.NORMAL,
        timeout_ms: int = 30000,
    ):
        self._registry = registry
        self.permission_level = permission_level
        self.timeout_ms = timeout_ms
        self._validator = SQLValidator()

    async def execute(
        self,
        sql: str,
        datasource: str | None = None,
        cancellation_event: asyncio.Event | None = None,
    ) -> QueryResult:
        """Execute SQL with safety checks and cancellation support.

        Args:
            sql: SQL to execute.
            datasource: Target datasource name.
            cancellation_event: Optional event to signal cancellation.

        Returns:
            QueryResult.

        Raises:
            SQLExecutionError: On execution failure.
            CancelledError: If cancelled during execution.
        """
        # Safety check — write operations are blocked in NORMAL and AUTO
        # modes; only DANGEROUS allows them through. DANGEROUS bypasses
        # the registry's read-only guard via execute_unsafe (the explicit
        # escape hatch for write-permission mode).
        if self.permission_level != PermissionLevel.DANGEROUS:
            if not self._validator.is_safe(sql):
                raise SQLExecutionError(
                    message="Write operations are not permitted under the "
                            "current permission level. Use /permission dangerous "
                            "to allow writes.",
                    sql=sql,
                )

        # Check cancellation before execution
        if cancellation_event and cancellation_event.is_set():
            raise CancelledError("Execution cancelled")

        # Execute with timeout
        execute = (
            self._registry.execute_unsafe
            if self.permission_level == PermissionLevel.DANGEROUS
            else self._registry.execute
        )
        try:
            if cancellation_event:
                # Execute in a task so we can cancel it
                task = asyncio.create_task(execute(sql, datasource))
                done, _ = await asyncio.wait(
                    [task],
                    timeout=self.timeout_ms / 1000.0,
                )
                if not done:
                    task.cancel()
                    raise asyncio.TimeoutError()

                # Check cancellation
                if cancellation_event.is_set():
                    raise CancelledError("Execution cancelled")

                return task.result()
            else:
                return await asyncio.wait_for(
                    execute(sql, datasource),
                    timeout=self.timeout_ms / 1000.0,
                )

        except asyncio.TimeoutError:
            raise SQLExecutionError(
                message=f"Query timed out after {self.timeout_ms}ms",
                sql=sql,
            )
        except (CancelledError, asyncio.CancelledError):
            raise CancelledError("Execution cancelled")

    async def stop_execute(self, datasource: str | None = None) -> None:
        """Request cancellation of a running query.

        This sets the cancellation event that the execute method checks.
        """
        logger.info("Stop execution requested for datasource: %s", datasource or "default")
