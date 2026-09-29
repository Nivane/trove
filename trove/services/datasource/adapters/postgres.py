"""PostgreSQL database adapter (psycopg async).

Introspection via information_schema + pg_catalog:
  - tables: pg_class/pg_namespace with reltuples (approximate row counts)
  - columns: information_schema.columns + primary-key join

The driver is imported lazily so the adapter module stays importable
without `uv sync --extra postgres`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

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
from trove.services.datasource.adapters.base import (
    INTERRUPT_TIMEOUT_S,
    DatabaseAdapter,
)

logger = get_logger(__name__)

DEFAULT_PORT = 5432


def _get_driver():
    """Import psycopg lazily (raises DatasourceError with a hint when missing)."""
    try:
        import psycopg
        return psycopg
    except ImportError as e:
        raise DatasourceError(
            message="psycopg is not installed — run `uv sync --extra postgres`",
            datasource="",
        ) from e


def _conninfo(config: dict[str, Any], credentials: dict[str, str] | None = None) -> str:
    """Build a psycopg conninfo string from connection params + credentials."""
    params = {**config, **(credentials or {})}
    host = params.get("host", "127.0.0.1")
    port = params.get("port", DEFAULT_PORT)
    user = params.get("user", "")
    password = params.get("password", "")
    database = params.get("database", "")
    auth = ""
    if user and password:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    elif user:
        auth = f"{quote(user, safe='')}@"
    elif password:
        auth = f":{quote(password, safe='')}@"
    return f"postgresql://{auth}{host}:{port}/{database}"


class PostgresAdapter(DatabaseAdapter):
    """PostgreSQL database adapter via psycopg (async)."""

    # 三处来源:pg_class.reltuples(行数)、pg_relation_size(字节)、
    # pg_stat_user_tables.last_analyze(统计收集时间)。
    # **没有 last_modified**:PG 的目录里没有「数据最后修改时间」这个字段,
    # 不拿 last_analyze 顶替 —— 那是「什么时候统计的」,不是「数据到哪儿了」。
    profile_capabilities = frozenset({"row_count", "bytes", "last_analyzed"})

    def __init__(self, name: str = "postgres", config: dict[str, Any] | None = None):
        super().__init__(name, config or {})
        self._conn: Any = None

    @staticmethod
    def dialect() -> str:
        return "postgres"

    async def connect(self) -> None:
        if self._connected:
            return
        try:
            psycopg = _get_driver()
            self._conn = await psycopg.AsyncConnection.connect(
                _conninfo(self.config, self.config.get("credentials")),
            )
            self._connected = True
            logger.debug("Connected to PostgreSQL: %s:%s/%s",
                         self.config.get("host", "127.0.0.1"),
                         self.config.get("port", DEFAULT_PORT),
                         self.config.get("database", ""))
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=(
                    f"Failed to connect to PostgreSQL at "
                    f"{self.config.get('host', '127.0.0.1')}:"
                    f"{self.config.get('port', DEFAULT_PORT)}: {e}"
                ),
                datasource=self.name,
            ) from e

    async def disconnect(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
        self._connected = False

    supports_interrupt = True

    async def interrupt(self) -> bool:
        """psycopg AsyncConnection.cancel() — 服务端取消在跑查询(psycopg 3.1+)。

        与设计 §7.3 的 ``pg_cancel_backend(pid)`` 有一处**有意不同**:协议级的
        CancelRequest 直接打在**当前这条连接**的后端上,不需要先从
        ``pg_stat_activity`` 查出 pid 再拼一条 SQL —— 少一次往返,也少掉
        R4 里「打错 pid 杀错会话」的整类风险(那个 pid 是别人给的,这个是
        连接自己带走的)。
        """
        try:
            if self._conn is not None and hasattr(self._conn, "cancel"):
                await asyncio.wait_for(
                    self._conn.cancel(), timeout=INTERRUPT_TIMEOUT_S,
                )
        except Exception as e:
            logger.warning("PostgreSQL interrupt failed: %s", e)
            return False
        return True

    async def _ensure_connected(self) -> None:
        """Ensure a live connection, transparently reconnecting a stale one.

        Postgres closes idle connections; a long-running `trove serve`
        must not turn every query into a raw driver exception. A closed
        connection is reopened via :meth:`connect` (reset the connected
        flag first so connect()'s guard doesn't short-circuit).
        """
        if not self._conn or not self._connected:
            raise DatasourceError(message="Not connected", datasource=self.name)
        if self._conn.closed:
            logger.info("PostgreSQL connection stale; reconnecting")
            self._conn = None
            self._connected = False
            await self.connect()

    async def execute(self, sql: str) -> QueryResult:
        if not self._conn or not self._connected:
            raise SQLExecutionError(message="Not connected to PostgreSQL", sql=sql)
        await self._ensure_connected()

        start = time.monotonic()
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(sql)
                rows = await cur.fetchall()
                columns = [d.name for d in (cur.description or [])]
                elapsed_ms = (time.monotonic() - start) * 1000
        except asyncio.CancelledError:
            # 客户端中止:psycopg cancel() 是服务端协议级取消(另开通道),
            # 正在跑的查询会真正停下。
            await self.interrupt()
            raise
        except Exception as e:
            raise SQLExecutionError(
                message=f"PostgreSQL execution error: {e}",
                sql=sql,
                db_error=str(e),
            ) from e

        return QueryResult(
            columns=columns,
            rows=[list(row) for row in rows],
            row_count=len(rows),
            execution_time_ms=round(elapsed_ms, 2),
            sql=sql,
            datasource=self.name,
        )

    async def get_schema(self) -> SchemaInfo:
        await self._ensure_connected()

        tables = []
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    "SELECT t.relname, t.reltuples::bigint AS row_count "
                    "FROM pg_class t "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "WHERE n.nspname = current_schema() AND t.relkind = 'r' "
                    "ORDER BY t.relname"
                )
                table_rows = await cur.fetchall()

                for tname, row_count in table_rows:
                    await cur.execute(
                        "SELECT c.column_name, c.data_type, c.is_nullable, "
                        "CASE WHEN pk.column_name IS NULL THEN '' ELSE 'PRI' END AS col_key "
                        "FROM information_schema.columns c "
                        "LEFT JOIN ("
                        "  SELECT kcu.column_name "
                        "  FROM information_schema.table_constraints tc "
                        "  JOIN information_schema.key_column_usage kcu "
                        "    ON tc.constraint_name = kcu.constraint_name "
                        "   AND tc.table_schema = kcu.table_schema "
                        "   AND tc.table_name = kcu.table_name "
                        "  WHERE tc.constraint_type = 'PRIMARY KEY' "
                        "    AND tc.table_schema = current_schema() "
                        "    AND tc.table_name = %s"
                        ") pk ON pk.column_name = c.column_name "
                        "WHERE c.table_schema = current_schema() AND c.table_name = %s "
                        "ORDER BY c.ordinal_position",
                        (tname, tname),
                    )
                    columns = [
                        ColumnInfo(
                            name=col[0],
                            type=str(col[1]),
                            nullable=(col[2] == "YES"),
                            primary_key=(col[3] == "PRI"),
                        )
                        for col in await cur.fetchall()
                    ]
                    tables.append(TableInfo(
                        name=tname,
                        schema=str(self.config.get("database", "")),
                        columns=columns,
                        # reltuples 在「从未 VACUUM/ANALYZE 过」时是 -1(官方
                        # 文档的哨兵),不是 0 —— 原样透出会让 catalog 报 -1 行。
                        row_count_estimate=positive_int(row_count),
                    ))
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"PostgreSQL schema introspection failed: {e}",
                datasource=self.name,
            ) from e

        return SchemaInfo(tables=tables)

    async def table_profiles(self) -> dict[str, TableProfile]:
        """画像:行数 + 表字节数 + 统计收集时间(§8.2 B 的方言实现)。

        **不与 ``get_schema`` 合并**:``get_schema`` 的列信息是每表一个往返,
        而画像要的三个量一次目录扫描就够。合并看着省事,代价是每次画像刷新
        (TTL 5min)都付一遍架构内省的钱(§10)。

        ``pg_relation_size`` 而不是 ``pg_total_relation_size``:后者含索引与
        TOAST,而这里要的是「顺序读这张表要碰多少字节」的量纲。用总量会高估一次
        不带索引的扫描。

        分区表(``relkind = 'p'``)本身没有存储,行数与字节都会是「没有依据」;
        真正的量在各分区上 —— 分区是 ``relkind = 'r'``,和普通表一起出现在
        结果里。
        """
        await self._ensure_connected()
        caps = self.profile_capabilities
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    "SELECT c.relname, c.reltuples::bigint AS row_count, "
                    "pg_relation_size(c.oid) AS bytes, "
                    "s.last_analyze "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
                    "WHERE n.nspname = current_schema() AND c.relkind = 'r' "
                    "ORDER BY c.relname"
                )
                rows = await cur.fetchall()
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"PostgreSQL profile introspection failed: {e}",
                datasource=self.name,
            ) from e

        return {
            str(name): TableProfile(
                table=str(name),
                row_count=positive_int(row_count),
                bytes=positive_int(size),
                last_analyzed=timestamp_str(last_analyzed),
                capabilities=caps,
            )
            for name, row_count, size, last_analyzed in rows
        }

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(
            supports_cte=True,
            supports_window_functions=True,
            supports_transactions=True,
            supports_json_type=True,
            dialect="postgres",
        )
