"""Snowflake database adapter (snowflake-connector-python, sync → asyncio.to_thread).

本仓第一个**同步驱动**适配器(驱动没有 asyncio 变体):所有 I/O 调用全部
``asyncio.to_thread`` 包住 —— 事件循环不被阻塞。而 asyncio 的取消只收回**等待**,
收不回工作线程:进程侧"取消"之后,服务端那条查询会继续跑,除非有人真的去停它。
∴ ``interrupt()`` 在这里不是优化,是唯一能停下来的手段。

终止机制 = **按 ``requestId`` 打 abort** —— 驱动自己的超时定时器与 SIGINT
处理器走的就是 ``conn._cancel_query(sql, request_id)`` 这一条路:

* ``execute`` 之前由**我们**生成 UUID4,经 ``_statement_params`` 注入(驱动在
  解析到合法 uuid4 时采用它);abort 时原样回传。名字双方都认得,才 abort 得动。
* 不用 ``cursor.abort_query(sfqid)``:那个接口要**服务端查询 id**,而同步
  ``execute`` 的 ``sfqid`` 要等 ``cmd_query`` 返回之后才被驱动赋值 ——
  查询在飞时它是 None,拿不到的就是拿不到。
* 注入依赖 4.0+ 的驱动行为(3.x 会另生成 id),``pyproject`` 的 floor 因此是
  ``>=4.0``;若在 3.x 上跑,abort 会被服务端拒 → 我们如实报 False,不是静默的谎。

Introspection via information_schema:两次查询(TABLES 带 ROW_COUNT + COLUMNS
一次取全按表分组)。**不与 MySQL 同构地逐表查列** —— 这里每个往返都是 REST
调用,贵一个数量级。

DB 侧语句超时随**登录参数** ``session_parameters.STATEMENT_TIMEOUT_IN_SECONDS``
装好(见 ``_statement_timeout_connect_kwargs``):闸在登录那一刻就位,重连自然
重装 —— 不存在「连上了但闸还没装」的窗口。

The driver is imported lazily so the adapter module stays importable
without `uv sync --extra snowflake`.

**诚实边界(未在真实云仓验证)**:单测钉的是**我们发出的形状**(假驱动),
服务端语义 —— abort 是否真让查询停下、STATEMENT_TIMEOUT 是否真生效 —— 本地
无云仓可验,只有 env-gated 的 ``SNOWFLAKE_TEST_URL`` 集成测试(CI 不跑)。
**不做**:SSO / 外部浏览器认证、连接池。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any

from trove.core.types import (
    Capabilities,
    ColumnInfo,
    QueryResult,
    SchemaInfo,
    TableInfo,
    positive_int,
)
from trove.core.errors import DatasourceError, SQLExecutionError
from trove.core.logging import get_logger
from trove.services.datasource.adapters.base import (
    INTERRUPT_TIMEOUT_S,
    DatabaseAdapter,
)

logger = get_logger(__name__)

#: 驱动在 ``execute`` 里认的注入名(``statement_params`` 含它 → ``self._request_id``
#: 采用这个值,并校验是合法 uuid4)。用字面量而不是 import:常量住在
#: ``snowflake.connector._utils``(私有模块),而值就是这一个字符串。哪天它变了,
#: 后果是 abort 打到一个服务端不认识的名字上 → 服务端回 ``success: false`` →
#: 我们如实报 False —— 响亮失败,不是静默的谎。
_REQUEST_ID_PARAM = "requestId"

#: 出错后**可以重发**的语句头(只读,幂等)。刻意不含 ``WITH``:Snowflake 的 CTE
#: 可以冠在 INSERT / UPDATE / DELETE / MERGE 前面,同一个前缀既可能是读也可能是
#: 写 —— 而「能不能重发」完全取决于这一点(与 MySQL 同一条理由)。
_READ_STATEMENT_HEADS = frozenset({"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"})

#: ``SELECT`` 外形、却有持久副作用的构造:``seq.NEXTVAL`` 会**推进序列**
#: (重发 = 静默多烧一个号)。它是 MySQL 那条 ``INTO OUTFILE`` 的对应物 ——
#: 同样是「读语句里的写」,同样按不重试处理。
_NON_IDEMPOTENT_MARKERS = ("NEXTVAL",)

#: 库/模式名会被**拼进** information_schema 查询(标识符没法参数化),所以先验
#: 形状:雪花未加引号的标识符 = 字母/下划线开头,后跟字母/数字/_/$。形状不对就
#: 拒绝,**不做转义** —— 加引号的雪花标识符大小写敏感,转义等于给这个名字第二
#: 种解释,而它只有一种正当解释。
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

#: 上一次终止的结果挂在 **asyncio 任务对象**上的属性名。
#:
#: 为什么必须留下这个结果:超时路径上 ``interrupt()`` 已被取消解栈调过一次
#: (那一刻 ``_inflight`` 里还有),而终止服务是**之后**才来问的 —— 那时登记
#: 已在 ``finally`` 里清空。不留结果,第二次只能回一个「本任务名下没有在飞的
#: 查询」的 ``True``,于是**「abort 被服务端拒绝」被报成 ``kill_sent``** ——
#: 证据恰好在它唯一想覆盖的场景里说谎。记在任务对象上是让这份记录天然属于
#: **一次请求**,跟着任务消失即自动回收。
_INTERRUPT_OUTCOME_ATTR = "_trove_snowflake_interrupt_outcome"


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


def _get_driver():
    """Import snowflake.connector lazily (raises DatasourceError with a hint when missing)."""
    try:
        import snowflake.connector
        return snowflake.connector
    except ImportError as e:
        raise DatasourceError(
            message="snowflake-connector-python is not installed — run `uv sync --extra snowflake`",
            datasource="",
        ) from e


def _is_read_statement(sql: str) -> bool:
    """语句本身是否幂等可重发(只读)。判据与理由见上方两个常量。"""
    text = str(sql or "").lstrip()
    # 跳过前导注释:``/* hint */ SELECT ...`` 与 ``-- note\nSELECT ...`` 都是读。
    # 注释没有闭合就不判读 —— 连语句头都取不到时按不可重试处理。
    while text:
        if text.startswith("/*"):
            end = text.find("*/", 2)
            if end < 0:
                return False
            text = text[end + 2:].lstrip()
        elif text.startswith("--"):
            end = text.find("\n")
            if end < 0:
                return False
            text = text[end + 1:].lstrip()
        else:
            break
    head = text.split(None, 1)[0].upper() if text else ""
    if head not in _READ_STATEMENT_HEADS:
        return False
    upper = text.upper()
    return not any(marker in upper for marker in _NON_IDEMPOTENT_MARKERS)


class SnowflakeAdapter(DatabaseAdapter):
    """Snowflake database adapter via snowflake-connector-python (sync → to_thread)."""

    label = "Snowflake"
    driver_hint = "`uv sync --extra snowflake`"

    # information_schema.TABLES.ROW_COUNT 是服务端的表级统计(视图为 NULL ——
    # 与「空表」是两个值,一律 positive_int 归一:不可得就是 None)。**没有**
    # bytes / last_modified:前者在 Snowflake 上没有与 MySQL 对等的口径(存储
    # 计费另算),后者的语义是元数据变更不是数据截止(§8.4 A)—— 声明它们就是
    # 替不存在的事实背书。
    profile_capabilities = frozenset({"row_count"})

    # 终止能力(§7.3):同步驱动包在 ``to_thread`` 里,取消只收回等待,工作线程
    # 与服务端查询都照跑 —— 主动终止是唯一能真停下来的手段(见模块 docstring)。
    supports_interrupt = True

    # DB 侧语句超时(§10):会话参数 STATEMENT_TIMEOUT_IN_SECONDS,随登录请求
    # 装好(见 ``_statement_timeout_connect_kwargs``)。进程被杀 / 事件循环卡死
    # 时,查询会在库里自己停下,而不是一直占着资源等一个不会再来的读取方。
    supports_statement_timeout = True

    # ── 并发:一条连接,多个协程 ────────────────────────────
    #
    # 一个适配器就是一个**共享单例**(registry 里按名字取同一个对象),而 agent
    # 循环会**并行派发工具**。同步驱动没有 async 锁可以替我们排队:两个协程各自
    # 进一个工作线程,同时对着同一条连接开游标 —— 与 MySQL 那场失步事故同一个
    # 根因,只是这里连协议层报错都不会有,直接是驱动内部状态互相踩。
    #
    # 修法同 MySQL:``_conn_lock`` 串行化所有触碰 ``self._conn`` 的段;公共入口
    # 持锁后调 ``_xxx_locked`` / 私有实现,私有实现里**不再取锁**(asyncio.Lock
    # 不可重入)。唯一例外是 ``interrupt``:**不取锁** —— 调用方是 ``execute``
    # 的取消栈,那一刻锁就在它手里;它只读纯内存登记,再沿同一条连接的 REST
    # 客户端发 abort(驱动自己的取消路径就是跨线程这么做的)。

    def __init__(self, name: str = "snowflake", config: dict[str, Any] | None = None):
        super().__init__(name, config or {})
        self._conn: Any = None
        self._conn_lock = asyncio.Lock()
        # 在飞查询:{asyncio 任务 → (sql, request_id)}。**按任务键**,不按适配器键:
        # 终止服务晚一点来问时必须只杀**本任务**那条查询。锁已经把同一时刻的查询
        # 数压到 1,但「只杀自己」这件事应该由结构保证,而不是由锁的时序保证。
        self._inflight: dict[Any, tuple[str, uuid.UUID]] = {}

    @staticmethod
    def dialect() -> str:
        return "snowflake"

    # ── 连接生命周期 ──────────────────────────────────────

    def _connect_kwargs(self) -> dict[str, Any]:
        """登录参数。**重连复用同一份**(闸随登录装好,见 ``_statement_timeout_connect_kwargs``)。"""
        account = str(self.config.get("account", "") or "").strip()
        if not account:
            raise DatasourceError(
                message="Snowflake config is missing the account identifier",
                datasource=self.name,
            )
        kwargs: dict[str, Any] = {
            "account": account,
            "user": self.config.get("user", ""),
            "password": self.config.get("password", ""),
            # 固定 pyformat:本适配器从不传绑定参数,选它是因为**行为确定** ——
            # params=None 时驱动不做 % 插值,SQL 里的字面 % 原样进服务端。
            "paramstyle": "pyformat",
        }
        # 只在给了值时才带上:缺省的交给驱动(如私钥认证在给 private_key_file
        # 时由驱动自己选择认证方式)。
        for key in ("database", "schema", "warehouse", "role", "private_key_file"):
            value = self.config.get(key)
            if value:
                kwargs[key] = str(value)
        port = self.config.get("port")
        if port:
            kwargs["port"] = int(port)
        # DB 侧兜底闸随**登录请求**一起装(session_parameters 是登录体的一部分):
        # 不存在「连上了但闸还没装」的窗口,重连也自动重装 —— 这是选连接参数
        # 形式、而非「连上后再发一条 ALTER SESSION」的全部理由。
        kwargs.update(self.statement_timeout_connect_kwargs())
        return kwargs

    def _statement_timeout_connect_kwargs(self, ms: int) -> dict[str, Any]:
        """``STATEMENT_TIMEOUT_IN_SECONDS``(**秒**)折进登录参数。

        ``max(1, ms // 1000)`` 兜住 ``ms < 1000``:这个会话参数上 **0 不是「关」**,
        按雪花文档是「按最大值(7 天)执行」—— 折成 0 会把一道闸变成一句空话。
        (ClickHouse 那边 0 是「无限制」,理由不同,结论同向:亚秒配置绝不许折零。)
        上限 604800s 交给服务端把关:超了登录直接报错 —— 响亮失败,好过静默缩水。
        """
        return {"session_parameters": {"STATEMENT_TIMEOUT_IN_SECONDS": max(1, ms // 1000)}}

    async def connect(self) -> None:
        async with self._conn_lock:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        """建连(**调用方必须已持有 ``_conn_lock``**)。

        登录本身就是探测:库/模式/仓库/角色不存在都会在这里失败(注册即探测,
        与 MySQL 的建连即探测同一个形状)。
        """
        if self._conn is not None and self._connected:
            return
        try:
            snowflake = _get_driver()
            self._conn = await asyncio.to_thread(
                snowflake.connect, **self._connect_kwargs(),
            )
            self._connected = True
            logger.debug("Connected to Snowflake: account=%s database=%s schema=%s",
                         self.config.get("account"), self.config.get("database"),
                         self.config.get("schema"))
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"Failed to connect to Snowflake "
                        f"({self.config.get('account')}): {e}",
                datasource=self.name,
            ) from e

    async def disconnect(self) -> None:
        # 关连接也走锁:否则会在一条正在跑的语句下方抽掉连接。
        async with self._conn_lock:
            await self._drop_connection()
            self._connected = False

    async def _drop_connection(self) -> None:
        """丢弃当前连接:**关闭、不复用**。

        ``SnowflakeConnection.close()`` 会发 delete-session 请求(网络 I/O)——
        必须进线程,不能直接调;``retry=False``:关的就是一条已知不可用的连接,
        不让 close 自己重试。``_connected`` 保持 True(与 MySQL 同):适配器仍在
        「应已连接」态,下一次使用会重建;只有显式 ``disconnect`` 才置 False。
        """
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await asyncio.to_thread(conn.close, retry=False)
            except Exception as e:   # 关一条已坏的连接失败,不改变"它已不可用"
                logger.debug("%s dropping connection: close failed: %s", self.label, e)

    async def _rebuild_connection(self) -> None:
        """丢弃旧连接并建一条新的;建不起来如实抛 ``reconnect failed``。"""
        await self._drop_connection()
        try:
            await self._connect_locked()
        except Exception as e:
            raise DatasourceError(
                message=f"{self.label} connection lost and reconnect failed: {e}",
                datasource=self.name,
            ) from e

    def _connection_unusable(self) -> bool:
        """连接是否已不可信(**纯属性读,零往返**)。

        * ``is_closed()`` —— 驱动把 rest 置空(客户端主动关 / 会话级失败);
        * ``expired``     —— 驱动在**主令牌过期**(GS 码 390114)时打的标;
          会话过期(390112)驱动自己会续期,不打这个标。两者都是驱动给的
          **事实**,不是我们从错误文本里猜的措辞。

        刻意不调 ``is_valid()``:那是每查一次多一个心跳往返,而驱动自己已经在
        维护会话保活 —— 多花的钱换不来等价的信息。
        """
        conn = self._conn
        if conn is None:
            return True
        return bool(conn.is_closed()) or bool(getattr(conn, "expired", False))

    async def _ensure_connected(self) -> None:
        """确保当前有一条可用连接(**调用方必须已持有 ``_conn_lock``**)。

        三种状态(与 MySQL 同构):
          * 从未连接 / 已显式 disconnect → ``Not connected``(不隐式建连);
          * 连接被丢弃过(``_conn is None`` 但 ``_connected``)→ 重建后即用;
          * 有连接 → 读两个免费标志(closed/expired),不可用就重建。

        没有 ``ping`` 这一步:驱动自己维护会话保活与续期,而上述两个标志把
        「连接已不可信」判得比 ping 更早 —— 还没有一条查询会因此白跑。
        """
        if self._conn is None:
            if not self._connected:
                raise DatasourceError(message="Not connected", datasource=self.name)
            await self._rebuild_connection()
            return
        if self._connection_unusable():
            logger.warning(
                "%s connection unusable (closed=%s, expired=%s); rebuilding it "
                "before running the statement",
                self.label, self._conn.is_closed(),
                bool(getattr(self._conn, "expired", False)),
            )
            await self._rebuild_connection()

    # ── 终止(§7.3 / I4)──────────────────────────────────

    async def interrupt(self) -> bool:
        """按 ``requestId`` abort 在飞查询(有界、绝不抛)。

        只杀**本任务**在飞的那条(登记见 ``_inflight``);问过一次就把结果记在
        本任务上(见 ``_INTERRUPT_OUTCOME_ATTR``),不再发第二次(§10 不重试 kill)。
        **刻意不取 ``_conn_lock``**:调用方是 ``execute`` 的取消栈,锁正在它手里。
        """
        task = asyncio.current_task()
        remembered = getattr(task, _INTERRUPT_OUTCOME_ATTR, None) if task else None
        if remembered is not None:
            return remembered
        entry = self._inflight.get(task) if task is not None else None
        if entry is None:
            # 本任务名下没有在飞的查询 —— 「本就无可取消」,与基类契约一致。
            return True
        sql, request_id = entry
        try:
            sent = await asyncio.wait_for(
                asyncio.to_thread(self._abort, sql, request_id),
                timeout=INTERRUPT_TIMEOUT_S,
            )
        except Exception as e:
            logger.warning("Snowflake interrupt failed: %s", e)
            return _remember_interrupt(task, False)
        if not sent:
            # 发得出去、服务端没收(拒了/认不得这个 requestId)—— 与「发不出去」
            # 同一条处置:如实报 False,但**必须留下 WARNING**。证据链的价值全在
            # 它不说谎:这里静默的话,`kill_failed` 在日志里查不到任何原因。
            logger.warning(
                "Snowflake abort for request %s was not accepted by the server",
                request_id,
            )
        return _remember_interrupt(task, bool(sent))

    def _abort(self, sql: str, request_id: uuid.UUID) -> bool:
        """在工作线程里发 abort;返回「服务端是否接下了这次终止」。

        ``conn._cancel_query`` 是驱动**自己**的取消实现(超时定时器与 SIGINT
        处理器都走它):POST ``/queries/v1/abort-request``,带原始 SQL 与
        requestId。没有连接可发 → False(「发不出去」,与「无可取消」不同)。
        """
        conn = self._conn
        if conn is None:
            return False
        ret = conn._cancel_query(sql, request_id)
        return bool(ret.get("success")) if isinstance(ret, dict) else False

    # ── 执行 ─────────────────────────────────────────────

    async def execute(self, sql: str) -> QueryResult:
        async with self._conn_lock:
            return await self._execute_locked(sql)

    async def _execute_locked(self, sql: str) -> QueryResult:
        if not self._conn and not self._connected:
            # 从未连接 / 已显式 disconnect:与历史一致地快速失败(不隐式建连)。
            raise SQLExecutionError(message="Not connected to Snowflake", sql=sql)

        # 连接层:closed/expired 会在这里被重建一次(语句尚未执行,任何语句都安全)。
        await self._ensure_connected()

        try:
            return await self._run_query(sql)
        except Exception as exc:
            if not self._connection_unusable():
                raise
            # 连接已不可信:**一律丢弃**(不复用),但**只有读语句重发** ——
            # 这条连接上「语句到底执行了没有」不可判,写语句重发可能产生第二次
            # 副作用,读语句重发是幂等的。丢弃不等于重发:写语句丢掉连接后直接
            # 把错误抛出去,由下一次调用用新连接。
            if not _is_read_statement(sql):
                await self._drop_connection()
                raise
            logger.warning(
                "%s connection became unusable while running a read statement "
                "(%s: %s); discarding it and retrying once on a fresh connection",
                self.label, type(exc).__name__, exc,
            )
            await self._rebuild_connection()
            try:
                return await self._run_query(sql)
            except Exception:
                if self._connection_unusable():
                    await self._drop_connection()   # 新连接也不可用:别留给下一条
                raise

    async def _run_query(self, sql: str) -> QueryResult:
        """在**当前**连接上跑一条语句(调用方已持锁、已确保连接可用)。

        游标生命周期整段在工作线程里:开游标 → 执行 → 取数 → 关游标(与 MySQL
        同:取消解栈先 abort 再让那次关闭尽快失败)。``requestId`` 由我们生成并
        经 ``_statement_params`` 注入 —— abort 才有一个双方都认识的名字。
        """
        start = time.monotonic()
        request_id = uuid.uuid4()
        task = asyncio.current_task()
        if task is not None:
            self._inflight[task] = (sql, request_id)
            # 上一次终止的结论属于**上一条查询**,新查询一开始就作废(不清的话,
            # 同一任务重试第二条查询时,``interrupt()`` 会拿着旧结论回话)。
            _forget_interrupt(task)

        def _run() -> tuple[list[str], list[list[Any]]]:
            cursor = self._conn.cursor()
            try:
                cursor.execute(
                    sql, _statement_params={_REQUEST_ID_PARAM: str(request_id)},
                )
                rows = cursor.fetchall()
                columns = [d[0] for d in cursor.description] if cursor.description else []
                return columns, [list(row) for row in rows]
            finally:
                cursor.close()

        try:
            columns, rows = await asyncio.to_thread(_run)
        except asyncio.CancelledError:
            # 客户端中止/超时取消:``to_thread`` 收不回那个线程,同步驱动会继续
            # 等一个不会再被读取的响应 —— 从另一个线程按 requestId 打 abort,
            # 正是驱动自己的超时定时器做的事(同一 REST 客户端、同一接口)。
            await self.interrupt()
            raise
        except Exception as e:
            raise SQLExecutionError(
                message=f"Snowflake execution error: {e}",
                sql=sql,
                db_error=str(e),
            ) from e
        finally:
            # 用完就清:留到下一次执行,下一次超时就会报到一个本任务名下不再
            # 在飞的查询上(「只杀自己」的另一半)。
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

    # ── 内省 ─────────────────────────────────────────────

    def _checked_identifier(self, value: str, what: str) -> str:
        """库/模式名验形状(理由见 ``_IDENT_RE``);返回原值供拼接。"""
        if not value:
            raise DatasourceError(
                message=f"Snowflake config is missing the {what} name",
                datasource=self.name,
            )
        if not _IDENT_RE.match(value):
            raise DatasourceError(
                message=f"Snowflake {what} name {value!r} is not a plain identifier "
                        f"(letters/digits/_/$, not starting with a digit) — refusing "
                        f"to splice it into an information_schema query",
                datasource=self.name,
            )
        return value

    async def get_schema(self) -> SchemaInfo:
        async with self._conn_lock:
            return await self._get_schema_locked()

    async def _get_schema_locked(self) -> SchemaInfo:
        await self._ensure_connected()

        database = self._checked_identifier(
            str(self.config.get("database", "") or ""), "database",
        )
        schema = self._checked_identifier(
            str(self.config.get("schema", "") or ""), "schema",
        )
        # 服务端把未加引号的标识符折叠成大写,information_schema 里存的就是大写
        # —— 比较字面量跟着折(库里查得到的名字只有这一种形态)。
        schema_literal = schema.upper()

        def _introspect() -> SchemaInfo:
            cursor = self._conn.cursor()
            try:
                cursor.execute(
                    f"SELECT TABLE_NAME, ROW_COUNT FROM {database}.INFORMATION_SCHEMA.TABLES "
                    f"WHERE TABLE_SCHEMA = '{schema_literal}' ORDER BY TABLE_NAME"
                )
                table_rows = cursor.fetchall()
                # 列一次取全再按表分组:每表一次 = N 个 REST 往返,而一次查询就能
                # 拿完整个模式(与 MySQL 逐表查列不同,理由见模块 docstring)。
                cursor.execute(
                    f"SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                    f"FROM {database}.INFORMATION_SCHEMA.COLUMNS "
                    f"WHERE TABLE_SCHEMA = '{schema_literal}' "
                    f"ORDER BY TABLE_NAME, ORDINAL_POSITION"
                )
                column_rows = cursor.fetchall()
            finally:
                cursor.close()

            by_table: dict[str, list[ColumnInfo]] = {}
            for tname, cname, data_type, is_nullable in column_rows:
                by_table.setdefault(str(tname), []).append(ColumnInfo(
                    name=str(cname),
                    type=str(data_type),
                    nullable=(str(is_nullable).upper() == "YES"),
                    # Snowflake 的约束**不强制**(字段元数据而非保证),把声明出来的
                    # 主键当事实报出去会误导生成 —— 一律不报。
                    primary_key=False,
                ))
            return SchemaInfo(tables=[
                TableInfo(
                    name=str(tname),
                    schema=schema,
                    columns=by_table.get(str(tname), []),
                    # 原样带出 NULL —— 「统计缺失/视图」与「空表」必须是两个值
                    row_count_estimate=positive_int(row_count),
                )
                for tname, row_count in table_rows
            ])

        try:
            return await asyncio.to_thread(_introspect)
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"Snowflake schema introspection failed: {e}",
                datasource=self.name,
            ) from e

    async def get_capabilities(self) -> Capabilities:
        # 无版本探测:CTE / 窗口 / 事务 / VARIANT 是全平台能力,不看账号版本。
        return Capabilities(
            supports_cte=True,
            supports_window_functions=True,
            supports_transactions=True,
            supports_json_type=True,
            dialect="snowflake",
        )
