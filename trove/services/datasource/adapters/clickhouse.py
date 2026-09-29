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
import uuid
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
from trove.services.datasource.adapters.base import (
    INTERRUPT_TIMEOUT_S,
    DatabaseAdapter,
)

logger = get_logger(__name__)

DEFAULT_PORT = 8123

#: 没有分区键的表在 system.parts 里的分区名(文档原话:``partition: tuple()``)。
#: 它是**哨兵**,不是取值 —— 放过去会被 freshness 当成数据的截止时间报出去。
_NO_PARTITION = "tuple()"

#: 上一次终止的结果挂在 **asyncio 任务对象**上的属性名。
#:
#: 为什么必须留下这个结果:超时路径上 ``interrupt()`` 已经被取消解栈调过一次,
#: 而终止服务是**之后**才来问的 —— 那时 ``_inflight`` 已经在 ``finally`` 里清空。
#: 不留结果,第二次只能回一个「本任务名下没有在飞的查询」的 ``True``,于是
#: **「KILL 被拒」被报成 ``kill_sent``** —— 证据恰好在它唯一想覆盖的场景里说谎。
#:
#: 为什么挂在任务对象上,而不是适配器里的第二本字典:这份记录天然属于**一次
#: 请求**,跟着任务消失即自动回收。用字典就得自己清理(漏一处就是慢泄漏),
#: 而且适配器实例被并发请求共用,A 的结果绝不能记到 B 头上 —— 挂任务上这两件事
#: 都自然成立。``_inflight`` 按任务键也是同一个理由。
_INTERRUPT_OUTCOME_ATTR = "_trove_clickhouse_interrupt_outcome"


def _remember_interrupt(task: Any, result: bool) -> bool:
    """记下这次终止的结果并原样返回(供 ``interrupt`` 出口调用)。"""
    if task is not None:
        setattr(task, _INTERRUPT_OUTCOME_ATTR, result)
    return result


def _forget_interrupt(task: Any) -> None:
    """作废本任务的终止结论 —— 新查询一开始就调,免得旧答案替新查询回话。"""
    if task is not None:
        try:
            delattr(task, _INTERRUPT_OUTCOME_ATTR)
        except AttributeError:
            pass


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

    # 终止能力(§7.3):服务端支持 ``KILL QUERY WHERE query_id = ...``,而本适配器
    # 用同步驱动包在 ``to_thread`` 里 —— 取消只收回**等待**,工作线程与服务端查询
    # 都照跑,所以主动终止在这里不是可选项,是唯一能真停下来的手段。
    supports_interrupt = True

    def __init__(self, name: str = "clickhouse", config: dict[str, Any] | None = None):
        super().__init__(name, config or {})
        self._client: Any = None
        # 在飞查询:{asyncio 任务 → query_id}。**按任务键**,不按适配器键:
        # 一个适配器实例被并发请求共用(``ConnectorRegistry.execute`` 没有
        # 串行化),单槽会在并发下把 B 的 query_id 覆盖成 A 的 —— 于是「精确
        # 匹配」变成「杀到别人」,正是 R4 要防的那件事。
        self._inflight: dict[Any, str] = {}

    @staticmethod
    def dialect() -> str:
        return "clickhouse"

    def _connect_kwargs(self) -> dict[str, Any]:
        """连接参数。**旁路连接必须与原连接同参**,否则是连到另一个库上去杀。"""
        return {
            "host": self.config.get("host", "127.0.0.1"),
            "port": self.config.get("port", DEFAULT_PORT),
            "username": self.config.get("user", ""),
            "password": self.config.get("password", ""),
            "database": self.config.get("database", "default"),
        }

    async def connect(self) -> None:
        if self._connected:
            return
        try:
            ch = _get_driver()
            self._client = await asyncio.to_thread(
                ch.get_client, **self._connect_kwargs(),
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

    async def interrupt(self) -> bool:
        """按 ``query_id`` 发 ``KILL QUERY``(设计 §7.3 / I4)。

        三条边界,都是刻意的:

        * **只杀本任务在飞的那条**。反过来,「这个适配器上有别人的查询在飞」
          **不是**这里该动的东西(R4)。
        * **问过一次就答那次的结果,不再发**(§10「不重试 kill」)。超时路径上
          取消解栈已经调过一次,``QueryTerminator`` 是**之后**来问的;那次结果
          记在本任务对象上(见 ``_INTERRUPT_OUTCOME_ATTR``),这里原样奉还。
          没有这条,第二次会看到 ``_inflight`` 已空,回一个「无可取消」的
          ``True`` —— 于是**「KILL 被拒」在证据里变成 ``kill_sent``**。
        * **旁路连接**。在跑的那个 client 正被那条查询占着,它发不出 KILL
          (与 MySQL 的 ``_kill_query`` 同构)。旁路连接用完即关。

        剩下那个 ``True``(无记录、也无在飞查询)说的是「本任务名下确实没有
        可取消的东西」—— 与基类契约一致,不算失败。
        """
        task = asyncio.current_task()
        remembered = getattr(task, _INTERRUPT_OUTCOME_ATTR, None) if task else None
        if remembered is not None:
            return remembered
        query_id = self._inflight.get(task)
        if not query_id:
            return True
        try:
            await asyncio.wait_for(
                self._kill_query(query_id), timeout=INTERRUPT_TIMEOUT_S,
            )
        except Exception as e:
            logger.warning("ClickHouse interrupt failed: %s", e)
            return _remember_interrupt(task, False)
        return _remember_interrupt(task, True)

    async def _kill_query(self, query_id: str) -> None:
        """``query_id`` 是本模块自己生成的 uuid4 hex,拼进 SQL 无注入面。"""
        ch = _get_driver()
        side = await asyncio.to_thread(ch.get_client, **self._connect_kwargs())
        try:
            await asyncio.to_thread(
                side.command, f"KILL QUERY WHERE query_id = '{query_id}'",
            )
        finally:
            await asyncio.to_thread(side.close)

    async def disconnect(self) -> None:
        if self._client:
            await asyncio.to_thread(self._client.close)
            self._client = None
        self._connected = False

    async def execute(self, sql: str) -> QueryResult:
        if not self._client or not self._connected:
            raise SQLExecutionError(message="Not connected to ClickHouse", sql=sql)

        start = time.monotonic()
        # 给服务端一个可以精确点名的对象(§7.3 / R4):没有它,超时后能做的
        # 只有「取消本地等待」,而查询在服务端继续烧资源。
        query_id = uuid.uuid4().hex
        task = asyncio.current_task()
        if task is not None:
            self._inflight[task] = query_id
            # 上一次终止的结论属于**上一条查询**,新查询一开始就作废。不清的话,
            # 同一个任务重试第二条查询时,``interrupt()`` 会拿着旧结论回话,
            # 甚至因此**不发**这次该发的 KILL(§10 说的是同一条查询不重发,
            # 不是「本任务这辈子只发一次」)。
            _forget_interrupt(task)

        def _run() -> tuple[list[str], list[list[Any]]]:
            try:
                # **必须走 ``settings=``**:驱动在查询路径上只读
                # ``context.settings``(clickhouse-connect 1.7.1 的
                # ``driver/_backendclient.py`` 组装 ``QueryRuntime`` 时),
                # ``transport_settings=`` 尽管接受 ``query_id`` 却**根本不被
                # 使用** —— 实测服务端 ``currentQueryID()`` 返回随机 UUID。
                # 那个写法不会报错,只会让超时后发出的 KILL 打在一个服务端
                # 不认识的 id 上:查询照跑,证据却写着 ``kill_sent``。R4 的
                # 「精确匹配」全靠这一行,所以它由集成测试在真库上钉着。
                result = self._client.query(sql, settings={"query_id": query_id})
                return list(result.column_names), [list(r) for r in result.result_rows]
            except Exception as e:
                raise SQLExecutionError(
                    message=f"ClickHouse execution error: {e}",
                    sql=sql,
                    db_error=str(e),
                ) from e

        try:
            columns, rows = await asyncio.to_thread(_run)
        except asyncio.CancelledError:
            # 客户端中止/超时取消:``to_thread`` 收不回那个线程,服务端查询也
            # 照跑 —— 停下来的唯一办法是从旁路连接按 query_id 打 KILL。
            await self.interrupt()
            raise
        except SQLExecutionError:
            raise
        except Exception as e:
            raise SQLExecutionError(
                message=f"ClickHouse execution error: {e}",
                sql=sql,
                db_error=str(e),
            ) from e
        finally:
            # 用完就清:留到下一次执行,下一次超时就会杀到别人的查询(R4)
            if task is not None:
                self._inflight.pop(task, None)

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
