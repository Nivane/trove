"""MySQL database adapter (aiomysql, async).

Introspection via information_schema:
  - tables: TABLE_NAME + TABLE_ROWS (approximate for InnoDB, fine for estimates)
  - columns: COLUMN_NAME / DATA_TYPE / IS_NULLABLE / COLUMN_KEY ('PRI' = PK)

The driver is imported lazily so the adapter module stays importable
without `uv sync --extra mysql`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

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

DEFAULT_PORT = 3306


class MySQLAdapter(DatabaseAdapter):
    """MySQL database adapter via aiomysql (async).

    Subclass overrides (Doris speaks the MySQL wire protocol):
      - ``label``          product name in connect/ping error messages
      - ``default_port``   server port when the config omits one
      - ``driver_hint``    pip/uv extra hint when aiomysql is missing
      - ``dialect()``      SQLGlot dialect
    """

    label = "MySQL"
    default_port = DEFAULT_PORT
    driver_hint = "`uv sync --extra mysql`"

    def __init__(self, name: str = "mysql", config: dict[str, Any] | None = None):
        super().__init__(name, config or {})
        self._conn: Any = None
        self._server_version = ""

    @classmethod
    def _get_driver(cls):
        """Import aiomysql lazily (raises DatasourceError with a hint when missing)."""
        try:
            import aiomysql
            return aiomysql
        except ImportError as e:
            raise DatasourceError(
                message=f"aiomysql is not installed — run {cls.driver_hint}",
                datasource="",
            ) from e

    @staticmethod
    def dialect() -> str:
        return "mysql"

    async def connect(self) -> None:
        if self._connected:
            return
        try:
            aiomysql = self._get_driver()
            self._conn = await aiomysql.connect(
                host=self.config.get("host", "127.0.0.1"),
                port=self.config.get("port", self.default_port),
                user=self.config.get("user", ""),
                password=self.config.get("password", ""),
                db=self.config.get("database", ""),
            )
            self._connected = True
            await self._probe_version()
            logger.debug("Connected to %s: %s:%s/%s",
                         self.label, self.config.get("host"), self.config.get("port"),
                         self.config.get("database"))
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"Failed to connect to {self.label} at "
                        f"{self.config.get('host')}:{self.config.get('port')}: {e}",
                datasource=self.name,
            ) from e

    async def _probe_version(self) -> None:
        """SELECT VERSION() — capabilities depend on major version (8.0+ has CTE/window)."""
        try:
            cursor = await self._conn.cursor()
            try:
                await cursor.execute("SELECT VERSION()")
                row = await cursor.fetchone()
                if row:
                    self._server_version = str(row[0])
            finally:
                await cursor.close()
        except Exception as e:
            logger.debug("Version probe failed (capabilities will be conservative): %s", e)

    async def disconnect(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
        self._connected = False

    async def interrupt(self) -> None:
        """KILL QUERY via a side connection (a busy connection can't serve it).

        Best-effort and bounded: thread-id lookup may be sync or coroutine
        across aiomysql versions, and any failure just logs at debug —
        the cancellation unwind must never hang.
        """
        try:
            await asyncio.wait_for(self._kill_query(), timeout=2.0)
        except Exception as e:
            logger.debug("MySQL interrupt failed (best-effort): %s", e)

    async def _kill_query(self) -> None:
        if self._conn is None:
            return
        getter = getattr(self._conn, "thread_id", None)
        if getter is None:
            return
        try:
            tid = getter() if not asyncio.iscoroutinefunction(getter) else await getter()
        except Exception:
            return
        if tid is None:
            return
        aiomysql = self._get_driver()
        side = await aiomysql.connect(
            host=self.config.get("host", "127.0.0.1"),
            port=self.config.get("port", self.default_port),
            user=self.config.get("user", ""),
            password=self.config.get("password", ""),
            db=self.config.get("database", ""),
        )
        try:
            cursor = await side.cursor()
            try:
                await cursor.execute(f"KILL QUERY {int(tid)}")
            finally:
                await cursor.close()
        finally:
            side.close()

    async def _ping_reconnect(self) -> None:
        """Reconnect if the underlying connection went stale.

        MySQL closes idle connections (wait_timeout); a long-running
        `trove serve` then turns every query/catalog call into a raw
        driver exception (InterfaceError/OperationalError). ping with
        reconnect transparently reopens the connection when the server
        is reachable again.
        """
        try:
            await self._conn.ping(reconnect=True)
        except Exception as e:
            raise DatasourceError(
                message=f"{self.label} connection lost and reconnect failed: {e}",
                datasource=self.name,
            ) from e

    async def _ensure_connected(self) -> None:
        """Ensure a live connection, reconnecting a stale one."""
        if not self._conn or not self._connected:
            raise DatasourceError(message="Not connected", datasource=self.name)
        await self._ping_reconnect()

    async def execute(self, sql: str) -> QueryResult:
        if not self._conn or not self._connected:
            raise SQLExecutionError(message="Not connected to MySQL", sql=sql)
        await self._ping_reconnect()

        start = time.monotonic()
        cursor = await self._conn.cursor()
        try:
            await cursor.execute(sql)
            rows = await cursor.fetchall()
            columns = [d[0] for d in cursor.description] if cursor.description else []
            elapsed_ms = (time.monotonic() - start) * 1000

            return QueryResult(
                columns=columns,
                rows=[list(row) for row in rows],
                row_count=len(rows),
                execution_time_ms=round(elapsed_ms, 2),
                sql=sql,
                datasource=self.name,
            )
        except asyncio.CancelledError:
            # 客户端中止:在跑连接发不了 KILL,走旁路连接 KILL QUERY
            # (同用户可杀自己的查询),服务端真正停止执行。
            await self.interrupt()
            raise
        except Exception as e:
            raise SQLExecutionError(
                message=f"MySQL execution error: {e}",
                sql=sql,
                db_error=str(e),
            ) from e
        finally:
            await cursor.close()

    async def get_schema(self) -> SchemaInfo:
        await self._ensure_connected()

        tables = []
        cursor = await self._conn.cursor()
        try:
            await cursor.execute(
                "SELECT TABLE_NAME, IFNULL(TABLE_ROWS, 0) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
            )
            table_rows = await cursor.fetchall()

            for tname, row_count in table_rows:
                await cursor.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY "
                    "FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                    "ORDER BY ORDINAL_POSITION",
                    (tname,),
                )
                columns = [
                    ColumnInfo(
                        name=col[0],
                        type=str(col[1]),
                        nullable=(col[2] == "YES"),
                        primary_key=(col[3] == "PRI"),
                    )
                    for col in await cursor.fetchall()
                ]
                tables.append(TableInfo(
                    name=tname,
                    schema=str(self.config.get("database", "")),
                    columns=columns,
                    row_count_estimate=int(row_count or 0),
                ))
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"MySQL schema introspection failed: {e}",
                datasource=self.name,
            ) from e
        finally:
            await cursor.close()

        return SchemaInfo(tables=tables)

    async def get_capabilities(self) -> Capabilities:
        try:
            major = int(self._server_version.split(".")[0])
        except (ValueError, IndexError):
            major = 0  # unknown → conservative
        return Capabilities(
            supports_cte=major >= 8,
            supports_window_functions=major >= 8,
            supports_transactions=True,
            supports_json_type=True,
            dialect="mysql",
        )
