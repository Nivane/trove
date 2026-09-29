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
from dataclasses import replace
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

#: 没有分区键的表在 system.parts 里的分区名(文档原话:``partition: tuple()``)。
#: 它是**哨兵**,不是取值 —— 放过去会被 freshness 当成数据的截止时间报出去。
_NO_PARTITION = "tuple()"


def _is_placeholder_partition(value: Any) -> bool:
    """``None`` / 空 / ``tuple()`` 都表示「这张表没有分区事实」。"""
    return value is None or str(value).strip() in ("", _NO_PARTITION)


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

    # 画像的字段来源:``system.tables``(行数 / 字节 / 分区键表达式)+
    # ``system.parts``(分区数 / 最新分区)。**没有 last_modified** —— 这个引擎
    # 上的「最后修改时间」反映的是元数据变更(加了个分区、改了 TTL),不代表数据
    # 到哪儿了(§8.4 A);分区才是它诚实的截止时间信号。
    profile_capabilities = frozenset({
        "row_count", "bytes", "partition_column",
        "partition_count", "latest_partition",
    })

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
                    row_count_estimate=positive_int(total_rows),
                ))
            return SchemaInfo(tables=tables)

        return await asyncio.to_thread(_introspect)

    async def table_profiles(self) -> dict[str, TableProfile]:
        """画像:行数 + 字节数 + 分区(§8.2 B 的方言实现)。

        两次 ``system`` 查询,不是每表一次。列名显式写进 SQL 而不是 ``SELECT *``:
        ``system.tables`` 是个上百列的系统表,取宽了在集群上很贵的。

        分区单独用一次 ``system.parts`` 查,**不与上面那个查询合并**:两个来源
        互不依赖,合并成 JOIN 会让分区块的失败连带丢掉行数与字节,把第 2 档整个
        作废。分开之后 ``system.parts`` 失败只退到「没有分区信息」(见下面的 catch)。
        """
        if not self._client or not self._connected:
            raise DatasourceError(message="Not connected", datasource=self.name)

        caps = self.profile_capabilities

        def _profile() -> dict[str, TableProfile]:
            res = self._client.query(
                "SELECT name, total_rows, total_bytes, partition_key FROM system.tables "
                "WHERE database = currentDatabase() AND name NOT LIKE '.%'"
            )
            profiles = {}
            for name, total_rows, total_bytes, partition_key in res.result_rows:
                # partition_key 是**表达式**(如 toYYYYMM(dt)),不是列名;
                # 没声明分区键时是空串。
                key = str(partition_key or "").strip()
                profiles[str(name)] = TableProfile(
                    table=str(name),
                    row_count=positive_int(total_rows),
                    bytes=positive_int(total_bytes),
                    partition_column=key or None,
                    capabilities=caps,
                )

            try:
                parts = self._client.query(
                    "SELECT table, uniqExact(partition) AS partition_count, "
                    "max(partition) AS latest_partition "
                    "FROM system.parts WHERE active AND database = currentDatabase() "
                    "GROUP BY table"
                )
            except Exception as e:
                # 权限不足 / 版本里没有 system.parts —— 少一个字段,不是放行:
                # 行数与字节照旧进预算判定。
                logger.warning("ClickHouse 分区画像失败,本档无分区依据: %s", e)
                return profiles

            for name, count, latest in parts.result_rows:
                if _is_placeholder_partition(latest):
                    continue
                p = profiles.get(str(name))
                if p is None:  # 内部表 / 临时表:system.tables 那一侧已经过滤掉
                    continue
                # 多表达式分区键上 max(partition) 是序列化元组的字符串比较,
                # 得到的是**任意**一个分区,不是最新的那个。分区数照报。
                latest_value = None if "," in (p.partition_column or "") else str(latest)
                profiles[str(name)] = replace(
                    p, partition_count=positive_int(count), latest_partition=latest_value,
                )
            return profiles

        return await asyncio.to_thread(_profile)

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(
            supports_cte=True,
            supports_window_functions=True,
            supports_transactions=False,  # ClickHouse has no multi-statement transactions
            supports_json_type=True,
            dialect="clickhouse",
        )
