"""Query cancellation: client abort must stop the datasource-side query.

The SSE teardown cancels the graph task, which propagates into
ConnectorRegistry.execute → adapter.execute; the adapter's CancelledError
handler fires the driver-level interrupt (sqlite3 interrupt / psycopg
cancel / KILL QUERY / duckdb interrupt) so the database stops working,
not just the awaiting coroutine.
"""

import asyncio
import logging

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

    async def test_a_failed_interrupt_reports_false_and_warns(
        self, sqlite_registry, caplog,
    ):
        """终止发不出去 → ``False`` + **WARNING**,且异常不逃出去(§10 / I4)。

        这条钉的是 P4 修的根因:存量的 ``interrupt`` 把失败吞进
        ``logger.debug``,于是「终止到底发出去没有」连日志里都查不到,而它正是
        超时证据要说清的那件事。返回 False 才让上游能记 ``kill_failed``。

        失败面是真的:连接在查询期间被关掉(或底层坏了)时,``aiosqlite`` 的
        ``_conn`` 属性**抛** ``ValueError`` 而不是返回 None —— 于是
        ``getattr(..., None)`` 的默认值救不了它,异常会一路冒到取消解栈里。
        ``interrupt`` 的「永不抛」契约就是为这种时候准备的。
        """
        adapter = await sqlite_registry.get("test_db")
        await adapter._conn.close()  # 只有底层连接坏掉这一种真实失败面

        with caplog.at_level(logging.WARNING):
            assert await adapter.interrupt() is False

        assert any("interrupt failed" in r.message for r in caplog.records)
