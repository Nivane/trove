"""Query cancellation: client abort must stop the datasource-side query.

The SSE teardown cancels the graph task, which propagates into
ConnectorRegistry.execute → adapter.execute; the adapter's CancelledError
handler fires the driver-level interrupt (sqlite3 interrupt / psycopg
cancel / KILL QUERY / duckdb interrupt) so the database stops working,
not just the awaiting coroutine.
"""

import asyncio

import pytest


class TestSqliteCancel:
    async def test_cancel_propagates_and_interrupts_query(self, sqlite_registry):
        # 递归 CTE 长跑查询:取消时几乎确定处于执行中(sum 1e9 行)
        sql = (
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
            "SELECT sum(x) FROM c LIMIT 1000000000"
        )
        task = asyncio.create_task(sqlite_registry.execute(sql, "test_db"))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # interrupt 后连接立即可用;若中断没生效,工作线程仍被长查询
        # 占住,这条 SELECT 1 会排队到超时——用 wait_for 兜底成干净失败
        result = await asyncio.wait_for(
            sqlite_registry.execute("SELECT 1", "test_db"), timeout=5
        )
        assert result.rows == [[1]]

    async def test_cache_hit_path_never_touches_adapter(self, sqlite_registry):
        """取消链路只影响真实执行;缓存命中短路,不产生数据库往返。"""
        await sqlite_registry.execute("SELECT 1", "test_db")
        result = await sqlite_registry.execute("SELECT 1", "test_db")
        assert result.rows == [[1]]
        assert sqlite_registry.result_cache_stats()["hits"] >= 1
