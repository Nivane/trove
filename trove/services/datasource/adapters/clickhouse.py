"""ClickHouse database adapter (clickhouse-connect).

clickhouse-connect is synchronous — every call is wrapped in
asyncio.to_thread so the async pipeline never blocks the loop.

Introspection via system tables:
  - system.tables: name + total_rows
  - system.columns: name / type / is_in_primary_key (ORDER BY key)
  - system.grants + system.role_grants: read-only role self-check (§4 I1)

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
    BASIS_GRANTS,
    BASIS_PROBE_FAILED,
    Capabilities,
    ColumnInfo,
    QueryResult,
    ReadonlyProbe,
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


# ── 只读角色自检(设计 §4 I1)──────────────────────────────

#: 算「写得动」的 ``access_type`` 字面量。**组别名与细粒度权限两种表示都要认**:
#:
#: * ``WRITE`` / ``READ`` 是**组别名**(``GRANT WRITE ON db.*`` 一把就是整组)。
#:   只读账号那侧对应的组别名是 ``READ``,它**不在**这个集合里 —— 进来就会让
#:   每个只读账号都被报成写得动,而那句 WARN 的全部价值来自它只在真能写时出现。
#: * ``INSERT`` / ``ALTER`` … 是**细粒度**权限。两种表示在真实授权里都会出现
#:   (``GRANT ALL`` 展开出来的是这一批),只认一种就会漏。
#:
#: 划线标准是**这条权限能不能改数据或改结构**。刻意不收的:
#:
#: * ``SYSTEM`` / ``KILL QUERY`` / ``KILL TRANSACTION`` —— 改的是**服务端状态**
#:   或别人的查询,不是数据。收了会让一批真的只读账号(给监控用的那种)在启动
#:   日志里 WARN,而一次没有依据的 WARN 会让真出现的那次也没人看。
#: * ``SHOW`` / ``SELECT`` / ``READ`` / ``dictGet`` / ``BACKUP`` —— 都是读。
#: * ``CLUSTER`` / ``FILE`` / ``URL`` / ``S3`` …(表函数与外部源)—— 读外部,
#:   不写本库。
#:
#: ``OPTIMIZE`` 收进来是被这条线逼出来的:它不改行,但会**按 TTL 真的删数据**
#: (``OPTIMIZE … FINAL`` 会物化 TTL),而只读边界该挡住的东西正是「数据会变」。
#: ``UNDROP TABLE`` 同理 —— 把删掉的表救回来也是改结构。
_CLICKHOUSE_WRITE_ACCESS_TYPES: tuple[str, ...] = (
    "WRITE",
    "INSERT",
    "ALTER",
    "CREATE",
    "DROP",
    "TRUNCATE",
    "OPTIMIZE",
    "UNDROP TABLE",
)

#: 上面三个父类型底下还有一批**子类型**,它们是同一个 Enum16 里**并列的独立取值**
#: (``system.grants.access_type`` 的枚举里能看到 ``ALTER UPDATE`` / ``ALTER
#: DELETE`` / ``ALTER TABLE`` / ``CREATE TABLE`` / ``DROP TABLE`` …)。
#:
#: 只比上面那 8 个字面量是不够的:一个「能改数据、但只授了 ``ALTER DELETE``」
#: 的账号会被数成**零条写权限**,于是 ``verified=true`` —— 而那正是这个探测唯一
#: 不许犯的错(替一道并不存在的边界背书)。
#:
#: 用前缀而不是把子类型列全:它们在版本之间会增删(25.12 的枚举里,以这三个词
#: 开头的取值除了三个父类型本身还有 67 个 —— 光 ALTER 一家的子类型就 48 个),
#: 列全了就是一枚版本炸弹:名字在某个版本上不存在,整条查询会被直接拒掉,探测
#: 于是**永远**只能报「不知道」。前缀不会:``toString`` 作用在 Enum16 上是合法的
#: (实测 25.12;``upper(access_type)`` 那种写法才是 code 43)。方向也是单边的:
#: **只会多报写得动,不会少报**。
_CLICKHOUSE_WRITE_ACCESS_PREFIXES: tuple[str, ...] = ("ALTER", "CREATE", "DROP")


def _write_predicate() -> str:
    """写权限的判定条件。**只此一份** —— 计数与证据列必须问同一个问题。

    两处各写一份的话,它们会在某次改动里分叉,而分叉的形态是「计数说有写权限、
    证据列出的是另一批」,看日志的人只会更糊涂。
    """
    literals = ", ".join(f"'{t}'" for t in _CLICKHOUSE_WRITE_ACCESS_TYPES)
    prefixes = [
        f"toString(access_type) LIKE '{p}%'" for p in _CLICKHOUSE_WRITE_ACCESS_PREFIXES
    ]
    return " OR ".join([f"access_type IN ({literals})", *prefixes])


def _readonly_probe_sql() -> str:
    """自检发出的**唯一**一条查询。返回「授权总数 / 写权限数 / 写权限类型名」。

    * ``access_type`` 只能用**字面量**直接比:这一列是 ``Enum16``,不是 String,
      ``upper(access_type)`` 在真库上直接报 code 43(实测 25.12)。代价是比不出
      子类型,那部分交给前缀(见上)。
    * ``WITH RECURSIVE`` 展开角色继承 —— ``system.grants`` 只列「授给这个用户」
      和「授给这个角色」两种行,继承**不会**自动展开。少了它,一个通过角色拿到
      ``INSERT`` 的账号会被判成只读,那是这个探测最危险的方向。
    * 三个数一次往返拿全,不是三次:``readonly.PROBE_TIMEOUT_S`` 只有 3 秒,而
      三个数都在同一张表上,分开查只是把同一遍扫描付三遍。
    """
    predicate = _write_predicate()
    return (
        "WITH RECURSIVE roles AS (\n"
        "    SELECT granted_role_name AS r FROM system.role_grants\n"
        "    WHERE user_name = currentUser()\n"
        "    UNION ALL\n"
        "    SELECT rg.granted_role_name FROM system.role_grants rg\n"
        "    JOIN roles ON rg.user_name = roles.r\n"
        ")\n"
        "SELECT count() AS total,\n"
        f"       countIf({predicate}) AS writes,\n"
        "       arrayStringConcat(arraySort(groupUniqArrayIf(\n"
        f"           toString(access_type), {predicate})), ', ') AS hits\n"
        "FROM system.grants\n"
        "WHERE (user_name = currentUser() OR role_name IN (SELECT r FROM roles))"
    )


#: 这条查询**覆盖不到什么**(都是知道并接受的,不是没想到):
#:
#: * ``is_partial_revoke`` —— 不做处理。被部分撤销的写权限仍然会被数成一条写
#:   权限,于是写计数偏高、结论偏向 ``False``(报「写得动」)。方向是安全的:
#:   宁可多一次 WARN,不可少一次。真要做对,得把 ``is_partial_revoke = 0``
#:   加进条件 —— 但那是把「撤销」的语义(DCL 的授予/撤销顺序)搬进这个探测里,
#:   收益是少一次 WARN,不值。
#: * ``access_object`` 与 ``database`` / ``table`` 的作用域 —— 不看。判定只问
#:   「这个账号**有没有**写权限」,不问「写在哪个库」:I1 断言的是「这道边界
#:   存在」,而一个能写别的库的账号并不是只读账号。代价是**不知道**这条写权限
#:   是不是落在本数据源那个库上,所以 WARN 里给不出该改哪一条授权。
#: * 服务端那条查询的**终止** —— 探测不带 ``query_id``(它不进 ``_inflight``,
#:   见 §7.3 的登记处),所以 3 秒超时收回的只是本地等待,服务端那条查询会自己
#:   跑完。它是只读且极廉价的聚合,这里不接 P4 的终止路径。
_READONLY_PROBE_SQL = _readonly_probe_sql()


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

    # DB 侧语句超时(§10):HTTP 协议没有可持久化的会话 SET,``max_execution_time``
    # 只能作为**请求级 setting** 随每次请求带上(见 ``_statement_timeout_connect_kwargs``)。
    # 服务端从第一条查询起就是有界的 —— 进程被杀 / 事件循环卡死时,查询会在
    # 库里自己停下,而不是一直占着资源等一个不会再来的读取方。
    supports_statement_timeout = True

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
        kwargs = {
            "host": self.config.get("host", "127.0.0.1"),
            "port": self.config.get("port", DEFAULT_PORT),
            "username": self.config.get("user", ""),
            "password": self.config.get("password", ""),
            "database": self.config.get("database", "default"),
        }
        # 请求级 setting 也走这里:折进同一份 kwargs,旁路 KILL 连接自动同参。
        kwargs.update(self.statement_timeout_connect_kwargs())
        return kwargs

    def _statement_timeout_connect_kwargs(self, ms: int) -> dict[str, Any]:
        """``max_execution_time`` 以**秒**计(ClickHouse 少数不用毫秒的设置);
        ``max(1, ms // 1000)`` 兜住 ``ms < 1000`` —— 折成 0 就是「无限制」,
        一道静默失效的闸比没有闸更糟。"""
        return {"settings": {"max_execution_time": max(1, ms // 1000)}}

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

    async def probe_readonly(self) -> ReadonlyProbe:
        """``system.grants``:这个账号到底能不能写(设计 §4 I1)。

        **只查权限表,绝不试写**。设计稿给的另一条路是「尝试一条必然失败的写
        语句」—— 它「必然失败」的前提正是「账号只读」这个待证假设:账号其实可写
        时,那条语句会**真的写进去**,一个探测变成一次生产写入。

        一次往返拿三个数(见 ``_READONLY_PROBE_SQL``):授权总数、写权限数、
        写权限的类型名。判定只看前两个,**证据取第三个** —— ``detail`` 只进日志,
        但「凭什么说它写得动」得答得出来。

        不接异常:查询被拒/连接断了,**如实往上抛**,由 ``readonly.probe`` 折成
        ``probe_failed``。在这里吞掉它就等于让「没查成」消失在实现里。

        自己折的只有**形状**那一处:聚合查询必然回一行三列,真回来别的形状说明
        这条路上拿到的不是它该拿到的东西(驱动换了、结果被拍平了)—— 那时候说
        「不知道」,不说「只读」。
        """
        if not self._client or not self._connected:
            raise DatasourceError(message="Not connected", datasource=self.name)

        def _run() -> Any:
            return self._client.query(_READONLY_PROBE_SQL).result_rows

        rows = await asyncio.to_thread(_run)

        row = rows[0] if rows else None
        if (
            not isinstance(row, (list, tuple))
            or len(row) < 3
            or not all(isinstance(v, int) for v in row[:2])
        ):
            return ReadonlyProbe(
                None, BASIS_PROBE_FAILED,
                f"system.grants probe returned an unexpected shape: {rows!r:.160}",
            )
        total, writes, hits = row

        if total == 0:
            # 一行授权都没有 ≠ 什么都不能干:更可能是这个账号的权限不来自 SQL
            # 授权(那它在这张表里就看不见)。零行支持不了任何结论 —— 往 True
            # 倒就是替一道并不存在的边界背书,而这正是 I1 要防的那个谎。
            return ReadonlyProbe(
                None, BASIS_PROBE_FAILED,
                "system.grants has no rows for the connected user",
            )
        if writes:
            return ReadonlyProbe(
                False, BASIS_GRANTS,
                f"{writes} of {total} grant(s) are write access types: {hits}",
            )
        return ReadonlyProbe(
            True, BASIS_GRANTS,
            f"{total} grant(s), none of them a write access type",
        )
