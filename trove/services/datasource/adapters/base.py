"""Abstract base class for all database adapters.

Each database type (SQLite, PostgreSQL, DuckDB, etc.)
implements this interface so the rest of the system
can interact with any database uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from trove.core.types import (
    Capabilities,
    QueryResult,
    SchemaInfo,
    TableProfile,
    positive_int,
)


class DatabaseAdapter(ABC):
    """Uniform interface for all database connectors.

    Subclasses must implement all abstract methods.
    Each adapter is responsible for:
      - Connection lifecycle (connect/disconnect)
      - Query execution
      - Schema introspection
      - Capability reporting
    """

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the database."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the database connection."""
        ...

    @abstractmethod
    async def execute(self, sql: str) -> QueryResult:
        """Execute a SQL query and return results.

        Args:
            sql: The SQL statement to execute (SELECT only for safety).

        Returns:
            QueryResult with columns, rows, and execution metadata.
        """
        ...

    async def interrupt(self) -> None:
        """Best-effort cancellation of the in-flight query (no-op default).

        Adapters whose driver exposes a cross-task cancel (sqlite3
        interrupt, psycopg cancel, MySQL KILL QUERY, duckdb interrupt)
        override this so a client abort stops the database work, not
        just the awaiting coroutine. Implementations must be bounded
        and must never raise — the caller is already unwinding a
        cancellation.
        """
        return None

    @abstractmethod
    async def get_schema(self) -> SchemaInfo:
        """Introspect the database and return full schema metadata."""
        ...

    # ── 执行画像(execution profile, §8.2 B)─────────────
    #
    # ``profile_capabilities`` 说的是**这个适配器实现了哪些字段的采集**,
    # 不是「某一行恰好有值」。两者的区别决定运维往哪儿修:
    #   - 没声明 → 能力问题(这个库/这条实现给不出)
    #   - 声明了但值是 None → 数据问题(这张表的统计信息没收集)
    #
    # ∴ 必须是**显式声明**,不能从值反推。基类默认**什么都不声明** ——
    # ``get_schema`` 虽是抽象方法,但它对 ``row_count_estimate`` 没有任何保证
    # (dataclass 默认就是 ``None``),替子类宣布「本库支持行数」会让第三方适配器
    # 带着一个谎出去。填了行数的适配器各自声明一行即可。
    profile_capabilities: frozenset[str] = frozenset()

    async def table_profiles(self) -> dict[str, TableProfile]:
        """批量表级画像,键为表名(设计 §9.2)。

        默认实现只复用 ``get_schema()`` 已经有的事实 —— 六个适配器全都填了
        ``row_count_estimate``,所以成本轨的第 2 档今天就能覆盖**全部方言**,
        不需要为它新写任何方言 SQL。

        **批量而非单表**(设计稿写的是 ``table_profile(table)``):单表签名会让
        画像服务按表名循环,一次刷新 = N 个往返,与 §10 的延迟约束打架。与既有
        ``get_schema()`` 同一种形状,一次刷新一个往返。覆盖它的方言实现同样只需
        一次查询(见 ClickHouse)。

        调用方(:class:`~trove.services.datasource.profile.ProfileService`)吃
        异常并退到下一档;这里**只管如实报**,不吞错。
        """
        schema = await self.get_schema()
        declared = frozenset(self.profile_capabilities)
        return {
            t.name: TableProfile(
                table=t.name,
                row_count=positive_int(t.row_count_estimate),
                capabilities=declared,
            )
            for t in schema.tables
        }

    @abstractmethod
    async def get_capabilities(self) -> Capabilities:
        """Report database capabilities (CTE, window functions, etc.)."""
        ...

    @staticmethod
    @abstractmethod
    def dialect() -> str:
        """Return the SQL dialect name for this database type.

        Examples: "sqlite", "postgres", "mysql", "snowflake".
        """
        ...

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args: Any):
        await self.disconnect()
