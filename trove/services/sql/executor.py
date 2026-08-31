"""SQL executor — thin proxy over ConnectorRegistry.

Adds permission checks and cancellation support
around the raw database execution.
"""

from __future__ import annotations

import asyncio
import contextlib
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
        # stop_execute 触发的取消信号,按数据源(默认 "" = default)分键;
        # execute 未收到外部事件时使用本表,事件随执行复用。
        self._cancellations: dict[str, asyncio.Event] = {}

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
        # stop_execute() 会 set 本表事件;外部事件(若有)由调用方管理。
        own_event = self._cancellations.setdefault(
            datasource or "", asyncio.Event()
        )
        event = cancellation_event or own_event
        try:
            if event.is_set():
                raise CancelledError("Execution cancelled")
            # 任务 + 事件竞速:任一先完成即返回。等待协程自身被取消时,
            # finally 保证任务/waiter 都被回收(原 asyncio.wait 写法会
            # 泄漏内部任务,让被中止的查询继续在后台跑完)。
            task = asyncio.create_task(execute(sql, datasource))
            waiter = asyncio.create_task(event.wait())
            try:
                done, _ = await asyncio.wait(
                    [task, waiter],
                    timeout=self.timeout_ms / 1000.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not task.done():
                    task.cancel()
                    # 等待取消落定(adapter 的 interrupt 钩子在此收尾),
                    # 不留悬空任务;结果无所谓,吞掉。
                    with contextlib.suppress(BaseException):
                        await task
                if not waiter.done():
                    waiter.cancel()

            if event.is_set():
                raise CancelledError("Execution cancelled")
            if task not in done:
                raise asyncio.TimeoutError()
            return task.result()

        except asyncio.TimeoutError:
            raise SQLExecutionError(
                message=f"Query timed out after {self.timeout_ms}ms",
                sql=sql,
            )
        except asyncio.CancelledError:
            # 等待方自己被取消(客户端中止):原样传播,不换成业务异常,
            # 让外层取消链/事件循环语义保持完整。
            raise
        except CancelledError:
            raise CancelledError("Execution cancelled")

    async def stop_execute(self, datasource: str | None = None) -> None:
        """Request cancellation of a running query.

        Sets the datasource's cancellation event, which execute() races
        against the query task; the winning event cancels the task so the
        adapter's interrupt hook stops the database-side work.
        """
        self._cancellations.setdefault(datasource or "", asyncio.Event()).set()
        logger.info("Stop execution requested for datasource: %s", datasource or "default")
