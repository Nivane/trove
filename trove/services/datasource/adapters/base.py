"""Abstract base class for all database adapters.

Each database type (SQLite, PostgreSQL, DuckDB, etc.)
implements this interface so the rest of the system
can interact with any database uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from trove.core.types import (
    BASIS_UNVERIFIABLE,
    Capabilities,
    QueryResult,
    ReadonlyProbe,
    SchemaInfo,
    TableProfile,
    positive_int,
)

#: 适配器自身终止调用的界(秒)。它是「等驱动确认把取消发出去」的耐心,不是
#: 业务参数 —— 所以各方言共用一个数,不放进配置。超界即放弃等待并如实
#: 返回 False(§10:不发第二次 kill,上层 ``QueryTerminator`` 另有硬超时兜底)。
INTERRUPT_TIMEOUT_S = 2.0

#: DB 侧语句超时的缺省界(毫秒)。**必须大于应用侧预算**(``budget.timeout_ms``
#: 默认 30s / 30000ms):应用侧才是主闸 —— 超时 → 取消 → ``QueryTerminator``
#: 的 KILL 证据链;库侧闸是「进程被杀 / 事件循环卡死、没人去取消」时的兜底。
#: 两者同值或库侧更小,主闸的证据链就永远没有机会生成,故障归因会从
#: 「Trove 杀了这条查询」悄悄变成「库自己超时了」。
#:
#: 连接参数 ``statement_timeout_ms`` 可覆盖:``<=0`` = 显式关闭(不发语句),
#: 非数 = 回到本缺省(配置写错不静默变成"关掉")。
DEFAULT_STATEMENT_TIMEOUT_MS = 60_000


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

    # ── DB 侧语句超时(§10 / I4)─────────────────────────
    #
    # 与 ``supports_interrupt`` 同一条纪律:**显式声明,不从行为反推**。
    # 应用侧的 ``wait_for`` 只在「连接还在、事件循环还转」时有效:进程被杀 /
    # 事件循环卡死时,库里的查询会一直跑(占着连接与资源,而 Trove 已经不
    # 知道它存在)。库侧超时是这一档的兜底,不是主闸 —— 缺省界大于应用侧
    # 预算正是为了不让它抢先(见 ``DEFAULT_STATEMENT_TIMEOUT_MS``)。
    #
    # 缺省 False 是安全方向:一个字都不发,退回既有取消链;替子类宣布支持
    # 却发不出语句,会让「以为有界其实没有」重新成立。声明与实现的同源性由
    # ``tests/services/test_adapter_statement_timeout_contract.py`` 钉住。
    supports_statement_timeout: bool = False

    def statement_timeout_ms(self) -> int | None:
        """本连接生效的 DB 侧语句超时(毫秒);``None`` = 不发任何语句。

        三态解析(连接参数 ``statement_timeout_ms``):缺省 → 全局缺省;
        ``<=0`` → 显式关闭(None);非数 → 回到缺省(写错的配置不许静默
        变成"关掉"——那正是配错时最不该拿到的结果)。未声明能力的方言恒
        None:没有机制可发,报一个数就是替不存在的闸背书。
        """
        if not self.supports_statement_timeout:
            return None
        raw = self.config.get("statement_timeout_ms", None)
        if raw is None:
            return DEFAULT_STATEMENT_TIMEOUT_MS
        try:
            ms = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_STATEMENT_TIMEOUT_MS
        return ms if ms > 0 else None

    def statement_timeout_connect_kwargs(self) -> dict[str, Any]:
        """连接参数形式的超时(pg ``options`` / clickhouse ``settings``)。

        不支持 / 未配置 → ``{}``:连接参数一字不加。旁路连接(ClickHouse 的
        KILL QUERY 连接)必须复用同一份 kwargs —— 否则是连到另一个库上去杀。
        """
        if not self.supports_statement_timeout:
            return {}
        ms = self.statement_timeout_ms()
        if ms is None:
            return {}
        return self._statement_timeout_connect_kwargs(ms)

    async def apply_statement_timeout(self) -> bool:
        """在连接建立**之后**把超时交给服务端(语句形式的方言:mysql)。

        与 ``interrupt`` 同一条纪律:有界、**不抛** —— 调用方在建连路径上,
        「兜底闸装不上」不该让整条连接失败(旧版本/同协议的其他引擎可能不认
        这条语句),但它必须是**看得见的**:服务端拒绝时打 warning 并返回
        False,绝不静默降级成"以为有界其实没有"。

        Returns:
            True  = 语句交给了服务端且无异议;
            False = 未声明能力 / 未配置 / 服务端拒绝(已打 warning)。
        """
        if not self.supports_statement_timeout:
            return False
        ms = self.statement_timeout_ms()
        if ms is None:
            return False
        return await self._apply_statement_timeout(ms)

    def _statement_timeout_connect_kwargs(self, ms: int) -> dict[str, Any]:
        """子类覆写点:把 ``ms`` 折成连接参数。基类:没有这种形式。"""
        return {}

    async def _apply_statement_timeout(self, ms: int) -> bool:
        """子类覆写点:把 ``ms`` 作为语句发给服务端。基类:什么都不发。"""
        return False

    async def probe_readonly(self) -> ReadonlyProbe:
        """这个连接上的账号是不是**确实只能读**(设计 §4 I1)。缺省:**不知道**。

        缺省值只有 ``None/unverifiable`` 一个诚实选项:

        * ``True`` 是谎 —— 把「我们没实现」渲染成「已确认只读」,等于替一道
          并不存在的硬边界背书,而 I1 的全部价值就是那道边界真的存在;
        * ``False`` 是误报 —— 一个没实现探测的适配器并不是「写得动」。

        实现者只允许走**查权限表**这条路(``SHOW GRANTS`` / ``system.grants`` /
        ``information_schema``),**永远不要试写一条「必然失败」的写语句**:它的
        必然性正来自「账号只读」这个待证假设,账号其实可写时它会真的写进去。
        """
        return ReadonlyProbe(None, BASIS_UNVERIFIABLE, "no probe implemented")

    async def table_profiles(self) -> dict[str, TableProfile]:
        """批量表级画像,键为表名(设计 §9.2)。

        默认实现只复用 ``get_schema()`` 已经有的事实 —— 七个适配器全都填了
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
