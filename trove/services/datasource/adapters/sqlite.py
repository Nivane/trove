"""SQLite database adapter.

The default adapter for local-first and demo scenarios.
Supports both file-based and in-memory (:memory:) databases.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiosqlite

from trove.core.types import (
    Capabilities,
    ColumnInfo,
    QueryResult,
    SchemaInfo,
    TableInfo,
)
from trove.core.errors import DatasourceError, SQLExecutionError
from trove.core.logging import get_logger
from trove.services.datasource.adapters.base import DatabaseAdapter

logger = get_logger(__name__)


class SQLiteAdapter(DatabaseAdapter):
    """SQLite database adapter via aiosqlite (async)."""

    # get_schema 用 SELECT COUNT(*)(精确值,不是估算)。
    profile_capabilities = frozenset({"row_count"})

    def __init__(self, name: str = "sqlite", config: dict[str, Any] | None = None):
        super().__init__(name, config or {})
        self._db_path: str = self.config.get("path", ":memory:")
        self._conn: aiosqlite.Connection | None = None

    @staticmethod
    def dialect() -> str:
        return "sqlite"

    async def connect(self) -> None:
        if self._connected:
            return
        try:
            # isolation_level=None → autocommit: each execute() is durably
            # committed. (Default legacy mode wraps DML in an implicit
            # transaction that is never committed and rolls back on close.)
            self._conn = await aiosqlite.connect(self._db_path, isolation_level=None)
            self._conn.row_factory = aiosqlite.Row
            self._connected = True
            logger.debug("Connected to SQLite: %s", self._db_path)
        except Exception as e:
            raise DatasourceError(
                message=f"Failed to connect to SQLite at {self._db_path}: {e}",
                datasource=self.name,
            ) from e

    async def disconnect(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
        self._connected = False

    async def execute(self, sql: str) -> QueryResult:
        if not self._conn or not self._connected:
            raise SQLExecutionError(
                message="Not connected to SQLite",
                sql=sql,
            )

        start = time.monotonic()
        try:
            cursor = await self._conn.execute(sql)
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            elapsed_ms = (time.monotonic() - start) * 1000

            # Convert Row objects to lists
            row_lists = [list(row) for row in rows]

            return QueryResult(
                columns=columns,
                rows=row_lists,
                row_count=len(row_lists),
                execution_time_ms=round(elapsed_ms, 2),
                sql=sql,
                datasource=self.name,
            )
        except asyncio.CancelledError:
            # 客户端中止:让底层 sqlite3 停在当前语句上,而不是让查询
            # 在后台线程里跑完。interrupt() 跨线程有效。
            await self.interrupt()
            raise
        except Exception as e:
            raise SQLExecutionError(
                message=f"SQLite execution error: {e}",
                sql=sql,
                db_error=str(e),
            ) from e

    supports_interrupt = True

    # DB 侧语句超时:**显式不声明**。SQLite 是文件内嵌引擎,没有服务端进程可以
    # 把查询停在库里 —— 进程一消失,后面的工作也不存在了。「进程被杀后查询还在
    # 跑」这个要防的失败模式在本地文件上没有对应物;声明 True 的唯一效果是发出
    # 一条没有意义的语句。取消链由 interrupt() 负责。
    supports_statement_timeout = False

    async def interrupt(self) -> bool:
        """sqlite3 interrupt — 跨线程取消底层正在执行的语句。

        注意:aiosqlite 的 interrupt() 是排队到同一个工作线程,查询
        卡住时永远排不到;必须从事件循环线程直接调底层 sqlite3
        连接的 interrupt()(sqlite3 明确支持跨线程调用)。

        不设 ``wait_for`` 界:这条调用是同步的(只置一个标志位就返回),
        没有任何可等待的东西可以超时 —— 给它套一层超时是装饰,不是保护。
        """
        try:
            if self._conn is not None:
                raw = getattr(self._conn, "_conn", None)
                if raw is not None:
                    raw.interrupt()
        except Exception as e:
            # warning 而非 debug:debug 意味着「这条路径出问题没人看得见」,
            # 而终止发不出去正是超时证据要说清的事(§10)
            logger.warning("SQLite interrupt failed: %s", e)
            return False
        return True

    async def get_schema(self) -> SchemaInfo:
        if not self._conn:
            raise DatasourceError(message="Not connected", datasource=self.name)

        # Get all user tables
        cursor = await self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        table_names = [row[0] for row in await cursor.fetchall()]

        tables = []
        for tname in table_names:
            info_cursor = await self._conn.execute(f"PRAGMA table_info('{tname}')")
            columns = []
            for col in await info_cursor.fetchall():
                columns.append(ColumnInfo(
                    name=col[1],
                    type=col[2],
                    nullable=not bool(col[3]),
                    primary_key=bool(col[5]),
                ))

            # Estimate row count
            count_cursor = await self._conn.execute(f"SELECT COUNT(*) FROM \"{tname}\"")
            row = await count_cursor.fetchone()
            row_count = row[0] if row else None

            tables.append(TableInfo(
                name=tname,
                schema="main",
                columns=columns,
                row_count_estimate=row_count,
            ))

        return SchemaInfo(tables=tables)

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(
            supports_cte=True,
            supports_window_functions=True,
            supports_transactions=True,
            supports_json_type=True,
            dialect="sqlite",
        )

    @property
    def db_path(self) -> str:
        return self._db_path
