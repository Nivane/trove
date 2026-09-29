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

#: 适配器自身终止调用的界(秒)。它是「等驱动确认把取消发出去」的耐心,不是
#: 业务参数 —— 所以六个方言共用一个数,不放进配置。超界即放弃等待并如实
#: 返回 False(§10:不发第二次 kill,上层 ``QueryTerminator`` 另有硬超时兜底)。
INTERRUPT_TIMEOUT_S = 2.0


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

    async def interrupt(self) -> bool:
        """Best-effort cancellation of the in-flight query (no-op default).

        Adapters whose driver exposes a cross-task cancel (sqlite3
        interrupt, psycopg cancel, MySQL KILL QUERY, duckdb interrupt)
        override this so a client abort stops the database work, not
        just the awaiting coroutine. Implementations must be bounded
        and must never raise — the caller is already unwinding a
        cancellation.

        Returns:
            True when the cancellation was handed to the driver without
            complaint **or when there was nothing in flight to cancel**;
            False when the driver reported a failure. It is deliberately
            *not* a confirmation that the server stopped — that would take
            another round trip (see ``services/sql/terminate.py``). The
            base default returns False: nothing was sent, and the
            capability is not declared (``supports_interrupt``).
        """
        return False

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

    # ── 终止能力(§7.3 / §10 / I4)────────────────────────
    #
    # 与 ``profile_capabilities`` 同一条纪律:**显式声明,不从行为反推**。
    # 声明 True 而 ``interrupt`` 是基类空实现 → 超时证据会写下 ``kill_sent``,
    # 而其实一个字都没发;声明 False 而其实实现了 → 能力白写且无人发现。
    # 两个方向都由 ``tests/services/test_adapter_interrupt_contract.py`` 钉住。
    #
    # 缺省 False 是安全方向:调用方退到「无法主动终止,asyncio cancel 已是
    # 能做的全部」(§10)。
    supports_interrupt: bool = False

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
