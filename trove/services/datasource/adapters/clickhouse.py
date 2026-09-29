"""ClickHouse database adapter (clickhouse-connect).

clickhouse-connect is synchronous — every call is wrapped in
asyncio.to_thread so the async pipeline never blocks the loop.

Introspection via system tables:
  - system.tables: name + total_rows
  - system.columns: name / type / is_in_primary_key (ORDER BY key)

The driver is imported lazily so the adapter module stays importable
without `uv sync --extra clickhouse`.
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
    TableProfile,
    positive_int,
)
from trove.core.errors import DatasourceError, SQLExecutionError
from trove.core.logging import get_logger
from trove.services.datasource.adapters.base import DatabaseAdapter

logger = get_logger(__name__)

DEFAULT_PORT = 8123


def _get_driver():
    """Import clickhouse_connect lazily (raises DatasourceError with a hint when missing)."""
    try:
        import clickhouse_connect
        return clickhouse_connect
    except ImportError as e:
        raise DatasourceError(
            message="clickhouse-connect is not installed — run `uv sync --extra clickhouse`",
            datasource="",
        ) from e


class ClickHouseAdapter(DatabaseAdapter):
    """ClickHouse database adapter via clickhouse-connect (sync → to_thread)."""

    # ``total_rows`` 与 ``total_bytes`` 同在 ``system.tables`` 一行里 —— 基线的
    # 一次查询顺带就拿到了字节数,不需要第二个往返。
    profile_capabilities = frozenset({"row_count", "bytes"})

    def __init__(self, name: str = "clickhouse", config: dict[str, Any] | None = None):
        super().__init__(name, config or {})
        self._client: Any = None

    @staticmethod
    def dialect() -> str:
        return "clickhouse"

    async def connect(self) -> None:
        if self._connected:
            return
        try:
            ch = _get_driver()
            self._client = await asyncio.to_thread(
                ch.get_client,
                host=self.config.get("host", "127.0.0.1"),
                port=self.config.get("port", DEFAULT_PORT),
                username=self.config.get("user", ""),
                password=self.config.get("password", ""),
                database=self.config.get("database", "default"),
            )
            self._connected = True
            logger.debug("Connected to ClickHouse: %s:%s/%s",
                         self.config.get("host"), self.config.get("port"),
                         self.config.get("database"))
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"Failed to connect to ClickHouse at "
                        f"{self.config.get('host')}:{self.config.get('port')}: {e}",
                datasource=self.name,
            ) from e

    async def disconnect(self) -> None:
        if self._client:
            await asyncio.to_thread(self._client.close)
            self._client = None
        self._connected = False

    async def execute(self, sql: str) -> QueryResult:
        if not self._client or not self._connected:
            raise SQLExecutionError(message="Not connected to ClickHouse", sql=sql)

        start = time.monotonic()

        def _run() -> tuple[list[str], list[list[Any]]]:
            try:
                result = self._client.query(sql)
                return list(result.column_names), [list(r) for r in result.result_rows]
            except Exception as e:
                raise SQLExecutionError(
                    message=f"ClickHouse execution error: {e}",
                    sql=sql,
                    db_error=str(e),
                ) from e

        try:
            columns, rows = await asyncio.to_thread(_run)
        except SQLExecutionError:
            raise
        except Exception as e:
            raise SQLExecutionError(
                message=f"ClickHouse execution error: {e}",
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
        if not self._client or not self._connected:
            raise DatasourceError(message="Not connected", datasource=self.name)

        def _introspect() -> SchemaInfo:
            tables_res = self._client.query(
                "SELECT name, total_rows FROM system.tables "
                "WHERE database = currentDatabase() AND name NOT LIKE '.%' "
                "ORDER BY name"
            )
            tables = []
            for tname, total_rows in tables_res.result_rows:
                cols_res = self._client.query(
                    "SELECT name, type, is_in_primary_key FROM system.columns "
                    "WHERE database = currentDatabase() AND table = {tbl:String} "
                    "ORDER BY position",
                    parameters={"tbl": tname},
                )
                tables.append(TableInfo(
                    name=tname,
                    schema=str(self.config.get("database", "")),
                    columns=[
                        ColumnInfo(
                            name=col[0],
                            type=str(col[1]),
                            nullable=True,  # ClickHouse has no NOT NULL by default
                            primary_key=bool(col[2]),
                        )
                        for col in cols_res.result_rows
                    ],
                    row_count_estimate=int(total_rows or 0),
                ))
            return SchemaInfo(tables=tables)

        return await asyncio.to_thread(_introspect)

    async def table_profiles(self) -> dict[str, TableProfile]:
        """画像:行数 + 字节数,一次 ``system.tables`` 查询(§8.2 B 的方言实现)。

        覆盖基线的理由不是「基类算不出行数」,而是**同一次查询里多一列就有字节
        数**:字节数正是成本轨最想要的量纲(``assume_max_scan_bytes`` 用的就是
        bytes),而基线给的只是行数。

        列名显式写进 SQL 而不是 ``SELECT *``:``system.tables`` 是个上百列的系统
        表,取宽了在集群上是很贵的。
        """
        if not self._client or not self._connected:
            raise DatasourceError(message="Not connected", datasource=self.name)

        caps = self.profile_capabilities

        def _profile() -> dict[str, TableProfile]:
            res = self._client.query(
                "SELECT name, total_rows, total_bytes FROM system.tables "
                "WHERE database = currentDatabase() AND name NOT LIKE '.%'"
            )
            return {
                str(name): TableProfile(
                    table=str(name),
                    row_count=positive_int(total_rows),
                    bytes=positive_int(total_bytes),
                    capabilities=caps,
                )
                for name, total_rows, total_bytes in res.result_rows
            }

        return await asyncio.to_thread(_profile)

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(
            supports_cte=True,
            supports_window_functions=True,
            supports_transactions=False,  # ClickHouse has no multi-statement transactions
            supports_json_type=True,
            dialect="clickhouse",
        )
