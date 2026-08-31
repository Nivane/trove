"""DuckDB database adapter (duckdb, sync → asyncio.to_thread).

Supports file paths and :memory: databases. Introspection:
  - tables: duckdb_tables()
  - row estimates: COUNT(*) per table (local files — cheap enough)
  - columns: PRAGMA table_info (name / type / notnull / pk)

The driver is imported lazily so the adapter module stays importable
without `uv sync --extra duckdb`.
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


def _get_driver():
    """Import duckdb lazily (raises DatasourceError with a hint when missing)."""
    try:
        import duckdb
        return duckdb
    except ImportError as e:
        raise DatasourceError(
            message="duckdb is not installed — run `uv sync --extra duckdb`",
            datasource="",
        ) from e


class DuckDBAdapter(DatabaseAdapter):
    """DuckDB database adapter via duckdb (sync → to_thread)."""

    def __init__(self, name: str = "duckdb", config: dict[str, Any] | None = None):
        super().__init__(name, config or {})
        self._db_path: str = self.config.get("path", ":memory:")
        self._conn: Any = None

    @staticmethod
    def dialect() -> str:
        return "duckdb"

    async def connect(self) -> None:
        if self._connected:
            return
        try:
            duckdb = _get_driver()
            self._conn = await asyncio.to_thread(
                duckdb.connect, database=self._db_path,
            )
            self._connected = True
            logger.debug("Connected to DuckDB: %s", self._db_path)
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"Failed to connect to DuckDB at {self._db_path}: {e}",
                datasource=self.name,
            ) from e

    async def disconnect(self) -> None:
        if self._conn:
            await asyncio.to_thread(self._conn.close)
            self._conn = None
        self._connected = False

    async def interrupt(self) -> None:
        """duckdb conn.interrupt() — 跨线程停止正在执行的语句。"""
        try:
            if self._conn is not None:
                await asyncio.to_thread(self._conn.interrupt)
        except Exception as e:
            logger.debug("DuckDB interrupt failed (best-effort): %s", e)

    async def execute(self, sql: str) -> QueryResult:
        if not self._conn or not self._connected:
            raise SQLExecutionError(message="Not connected to DuckDB", sql=sql)

        start = time.monotonic()

        def _run() -> tuple[list[str], list[list[Any]]]:
            try:
                relation = self._conn.execute(sql)
                columns = [d[0] for d in relation.description] if relation.description else []
                rows = [list(r) for r in relation.fetchall()]
                return columns, rows
            except Exception as e:
                raise SQLExecutionError(
                    message=f"DuckDB execution error: {e}",
                    sql=sql,
                    db_error=str(e),
                ) from e

        try:
            columns, rows = await asyncio.to_thread(_run)
        except asyncio.CancelledError:
            # 中止:to_thread 的线程不会被取消,duckdb interrupt() 跨线程
            # 让正在执行的语句尽快停下(后台线程随后自行回收)。
            await self.interrupt()
            raise
        except SQLExecutionError:
            raise
        except Exception as e:
            raise SQLExecutionError(
                message=f"DuckDB execution error: {e}",
                sql=sql,
                db_error=str(e),
            ) from e

        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            execution_time_ms=round((time.monotonic() - start) * 1000, 2),
            sql=sql,
            datasource=self.name,
        )

    async def get_schema(self) -> SchemaInfo:
        if not self._conn or not self._connected:
            raise DatasourceError(message="Not connected", datasource=self.name)

        def _introspect() -> SchemaInfo:
            tables = []
            names = [
                r[0] for r in self._conn.execute(
                    "SELECT table_name FROM duckdb_tables() "
                    "WHERE schema_name = 'main' AND table_name NOT LIKE '%_metadata' "
                    "ORDER BY table_name"
                ).fetchall()
            ]
            for tname in names:
                row_count = self._conn.execute(
                    f'SELECT COUNT(*) FROM "{tname}"'
                ).fetchall()[0][0]
                pragma_rows = self._conn.execute(
                    f'PRAGMA table_info("{tname}")'
                ).fetchall()
                tables.append(TableInfo(
                    name=tname,
                    schema="main",
                    columns=[
                        ColumnInfo(
                            name=row[1],
                            type=str(row[2]),
                            nullable=not bool(row[3]),
                            primary_key=bool(row[5]),
                        )
                        for row in pragma_rows
                    ],
                    row_count_estimate=int(row_count or 0),
                ))
            return SchemaInfo(tables=tables)

        return await asyncio.to_thread(_introspect)

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(
            supports_cte=True,
            supports_window_functions=True,
            supports_transactions=True,
            supports_json_type=True,
            dialect="duckdb",
        )
