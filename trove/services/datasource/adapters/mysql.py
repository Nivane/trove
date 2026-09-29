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
from collections.abc import Callable
from typing import Any

from trove.core.types import (
    Capabilities,
    ColumnInfo,
    QueryResult,
    SchemaInfo,
    TableInfo,
    TableProfile,
    positive_int,
    timestamp_str,
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

    # get_schema 取 information_schema.TABLES 的 row_count / DATA_LENGTH /
    # UPDATE_TIME。三者都有文档写明的失效方式 —— 行数在统计未收集时是 NULL、
    # 字节数只算聚簇索引、时间戳只对非分区 InnoDB 表给值且重启后不持久 ——
    # 所以一律经 positive_int / timestamp_str 归一:不可得就是 None。
    profile_capabilities = frozenset({"row_count", "bytes", "last_modified"})

    #: 画像的额外列:(SQL 列名, 字段名, 取值归一函数)。
    #: **声明与实现同源** —— ``table_profiles`` 从这个元组生成 SELECT 列清单。
    #: 继承者若给不出这些列(Doris 的 information_schema 是 FE 虚拟表,这两列
    #: 的值无从验证),把它清空即可,连查都不会去查 —— 免得能力矩阵说没有、
    #: 实现照样去查、值再流进 data_as_of。
    _PROFILE_COLS: tuple[tuple[str, str, Callable[[Any], Any]], ...] = (
        ("DATA_LENGTH", "bytes", positive_int),
        ("UPDATE_TIME", "last_modified", timestamp_str),
    )

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
                "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
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
                    # 原样带出 NULL —— 「统计缺失」与「空表」必须是两个值
                    row_count_estimate=positive_int(row_count),
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

    async def table_profiles(self) -> dict[str, TableProfile]:
        """画像:行数 + 表字节数 + 最近写入时间(§8.2 B 的方言实现)。

        一次 ``information_schema.TABLES`` 拿全。**不与 ``get_schema`` 合并**:
        列信息是每表一次 ``COLUMNS`` 查询,画像要的三个量一次就够,合并等于每次
        刷新都付一遍架构内省的钱(§10)。

        ``UPDATE_TIME`` 取到值时是**下界**(I_S 的统计列默认缓存 24h,change
        buffer 又会让它偏旧),方向安全:宁可说「数据截至更早」,不可说更新。
        """
        await self._ensure_connected()
        caps = self.profile_capabilities
        cols = ["TABLE_NAME", "TABLE_ROWS", *(c for c, _, _ in self._PROFILE_COLS)]

        cursor = await self._conn.cursor()
        try:
            await cursor.execute(
                f"SELECT {', '.join(cols)} FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
            )
            rows = await cursor.fetchall()
        except Exception as e:
            raise DatasourceError(
                message=f"{self.label} profile introspection failed: {e}",
                datasource=self.name,
            ) from e
        finally:
            await cursor.close()

        out: dict[str, TableProfile] = {}
        for row in rows:
            name, table_rows, *extras = row
            fields: dict[str, Any] = {"row_count": positive_int(table_rows)}
            for (_, field, coerce), raw in zip(self._PROFILE_COLS, extras):
                fields[field] = coerce(raw)
            out[str(name)] = TableProfile(table=str(name), capabilities=caps, **fields)
        return out

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
