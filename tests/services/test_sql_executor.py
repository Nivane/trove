"""SQL executor tests."""

import asyncio

import pytest

from trove.core.errors import SQLExecutionError, CancelledError
from trove.services.sql.executor import SQLExecutor, PermissionLevel


class TestSQLExecutor:
    async def test_execute_select(self, sqlite_registry):
        executor = SQLExecutor(
            registry=sqlite_registry,
            permission_level=PermissionLevel.AUTO,
        )
        result = await executor.execute("SELECT COUNT(*) FROM students")
        assert result.row_count == 1
        assert result.rows == [[5]]

    async def test_execute_with_datasource_name(self, sqlite_registry):
        executor = SQLExecutor(
            registry=sqlite_registry,
            permission_level=PermissionLevel.AUTO,
        )
        result = await executor.execute("SELECT name FROM students WHERE grade > 90", "test_db")
        assert result.row_count == 3

    async def test_normal_permission_blocks_write(self, sqlite_registry):
        executor = SQLExecutor(
            registry=sqlite_registry,
            permission_level=PermissionLevel.NORMAL,
        )
        with pytest.raises(SQLExecutionError):
            await executor.execute("DELETE FROM students")

    async def test_auto_permission_blocks_write(self, sqlite_registry):
        """AUTO mode auto-approves reads but write ops still raise."""
        executor = SQLExecutor(
            registry=sqlite_registry,
            permission_level=PermissionLevel.AUTO,
        )
        # Read OK
        result = await executor.execute("SELECT 1")
        assert result.rows == [[1]]
        # Write blocked
        with pytest.raises(SQLExecutionError):
            await executor.execute("DROP TABLE students")

    async def test_dangerous_permission_allows_all(self, sqlite_registry):
        executor = SQLExecutor(
            registry=sqlite_registry,
            permission_level=PermissionLevel.DANGEROUS,
        )
        # Create a table (write op) — allowed in dangerous mode
        await executor.execute("CREATE TABLE tmp_test (id INTEGER)")
        result = await executor.execute("SELECT name FROM sqlite_master WHERE name='tmp_test'")
        assert result.row_count == 1

    async def test_execution_error_raises(self, sqlite_registry):
        executor = SQLExecutor(
            registry=sqlite_registry,
            permission_level=PermissionLevel.AUTO,
        )
        with pytest.raises(SQLExecutionError) as exc_info:
            await executor.execute("SELECT * FROM nonexistent")
        assert "nonexistent" in str(exc_info.value)

    async def test_cancelled_before_execution(self, sqlite_registry):
        executor = SQLExecutor(registry=sqlite_registry)
        event = asyncio.Event()
        event.set()  # pre-set = cancelled

        with pytest.raises(CancelledError):
            await executor.execute("SELECT * FROM students", cancellation_event=event)

    async def test_timeout(self, sqlite_registry):
        executor = SQLExecutor(
            registry=sqlite_registry,
            permission_level=PermissionLevel.AUTO,
            timeout_ms=1,  # 1ms — virtually guaranteed to timeout
        )
        # Use a query that takes longer than 1ms
        with pytest.raises(SQLExecutionError) as exc_info:
            await executor.execute(
                "WITH RECURSIVE cnt(x) AS "
                "(SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x < 100000) "
                "SELECT MAX(x) FROM cnt"
            )
        assert "timed out" in str(exc_info.value).lower()

    async def test_stop_execute_noop(self, sqlite_registry):
        executor = SQLExecutor(registry=sqlite_registry)
        await executor.stop_execute()  # Should not raise


class TestSQLExecutorCancellation:
    """stop_execute / 等待方取消 —— 查询必须真正停下,不留悬空任务。"""

    async def test_stop_execute_cancels_running_query(self):
        class BlockingRegistry:
            async def execute(self, sql, datasource=None):
                await asyncio.Event().wait()

        executor = SQLExecutor(
            registry=BlockingRegistry(),
            permission_level=PermissionLevel.AUTO,
        )
        task = asyncio.create_task(executor.execute("SELECT 1"))
        await asyncio.sleep(0.05)
        await executor.stop_execute()
        with pytest.raises(CancelledError):
            await task

    async def test_awaiting_cancellation_cancels_inner_task(self):
        """等待协程被取消时,内部查询任务必须一并取消(原写法会泄漏)。"""

        class BlockingRegistry:
            def __init__(self):
                self.inner_cancelled = False

            async def execute(self, sql, datasource=None):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.inner_cancelled = True
                    raise

        reg = BlockingRegistry()
        executor = SQLExecutor(
            registry=reg, permission_level=PermissionLevel.AUTO,
        )
        task = asyncio.create_task(executor.execute("SELECT 1"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert reg.inner_cancelled is True

    async def test_stop_before_execution_raises(self, sqlite_registry):
        executor = SQLExecutor(registry=sqlite_registry)
        await executor.stop_execute("test_db")
        with pytest.raises(CancelledError):
            await executor.execute("SELECT 1", "test_db")


class TestPermissionLevels:
    def test_enum_values(self):
        assert PermissionLevel.NORMAL.value == "normal"
        assert PermissionLevel.AUTO.value == "auto"
        assert PermissionLevel.DANGEROUS.value == "dangerous"
