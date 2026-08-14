"""MySQL database adapter (aiomysql, async).

Introspection via information_schema:
  - tables: TABLE_NAME + TABLE_ROWS (approximate for InnoDB, fine for estimates)
  - columns: COLUMN_NAME / DATA_TYPE / IS_NULLABLE / COLUMN_KEY ('PRI' = PK)

The driver is imported lazily so the adapter module stays importable
without `uv sync --extra mysql`.
"""

from __future__ import annotations

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


def _get_driver():
    """Import aiomysql lazily (raises DatasourceError with a hint when missing)."""
    try:
        import aiomysql
        return aiomysql
    except ImportError as e:
        raise DatasourceError(
            message="aiomysql is not installed — run `uv sync --extra mysql`",
            datasource="",
        ) from e


class MySQLAdapter(DatabaseAdapter):
    """MySQL database adapter via aiomysql (async)."""

    def __init__(self, name: str = "mysql", config: dict[str, Any] | None = None):
        super().__init__(name, config or {})
        self._conn: Any = None
        self._server_version = ""

    @staticmethod
    def dialect() -> str:
        return "mysql"

    async def connect(self) -> None:
        if self._connected:
            return
        try:
            aiomysql = _get_driver()
            self._conn = await aiomysql.connect(
                host=self.config.get("host", "127.0.0.1"),
                port=self.config.get("port", DEFAULT_PORT),
                user=self.config.get("user", ""),
                password=self.config.get("password", ""),
                db=self.config.get("database", ""),
            )
            self._connected = True
            await self._probe_version()
            logger.debug("Connected to MySQL: %s:%s/%s",
                         self.config.get("host"), self.config.get("port"),
                         self.config.get("database"))
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"Failed to connect to MySQL at "
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

    async def execute(self, sql: str) -> QueryResult:
        if not self._conn or not self._connected:
            raise SQLExecutionError(message="Not connected to MySQL", sql=sql)

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
        except Exception as e:
            raise SQLExecutionError(
                message=f"MySQL execution error: {e}",
                sql=sql,
                db_error=str(e),
            ) from e
        finally:
            await cursor.close()

    async def get_schema(self) -> SchemaInfo:
        if not self._conn or not self._connected:
            raise DatasourceError(message="Not connected", datasource=self.name)

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
