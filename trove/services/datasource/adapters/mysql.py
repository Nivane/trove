"""MySQL database adapter (aiomysql, async).

Introspection via information_schema:
  - tables: TABLE_NAME + TABLE_ROWS (approximate for InnoDB, fine for estimates)
  - columns: COLUMN_NAME / DATA_TYPE / IS_NULLABLE / COLUMN_KEY ('PRI' = PK)

The driver is imported lazily so the adapter module stays importable
without `uv sync --extra mysql`.

并发:一个适配器 = **一条**连接,而调用方会并行使用它(agent 循环同轮
``asyncio.gather`` 派发 probe_query / check_result)。所有触碰 ``self._conn``
的段由每适配器一把 ``asyncio.Lock`` 串行化;连接级故障有**有界**恢复
(见 ``_conn_lock`` 与 ``_execute_locked`` 的注释)。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
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
    timestamp_str,
)
from trove.core.errors import DatasourceError, SQLExecutionError
from trove.core.logging import get_logger
from trove.services.datasource.adapters.base import (
    INTERRUPT_TIMEOUT_S,
    DatabaseAdapter,
)

logger = get_logger(__name__)

DEFAULT_PORT = 3306

#: ``SHOW GRANTS`` 里算「写得动」的权限名。
#:
#: 只看 `` ON `` **之前**那一段(权限清单真正所在的位置):表名与账号名都在它
#: 后面,``GRANT SELECT ON `db`.`insert_log``` 里的 ``insert_log`` 是表名 ——
#: 整行扫关键字的实现会把一个纯只读账号报成可写。
#:
#: ``LOCK TABLES`` / ``REFERENCES`` 也收进来:它们不改数据,但都不是只读边界
#: 该有的东西,而 I1 断言的正是「这道边界存在」。
_MYSQL_WRITE_PRIVILEGES = frozenset({
    "ALL PRIVILEGES", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER",
    "TRUNCATE", "CREATE TEMPORARY TABLES", "LOCK TABLES", "REFERENCES",
})


# ── 连接级故障的**收窄**判定(重试边界的第一半)─────────────
#
# 判定分两层,缺一不可(见 ``_execute_locked``):
#   1. 错误是不是「这条连接已不可信」(本段);
#   2. 语句本身能不能安全重发(``_is_retryable_read``)。
#
# 只看第一层会在写语句上重发(可能已经在服务端落了一半);只看第二层会
# 在一个语法错误上白拆一条好连接。两层都过才重建重试一次。
#
# 错误码优先(pymysql/aiomysql 把 ``(errno, message)`` 放在 args):
#
#   2006 CR_SERVER_GONE_ERROR      服务端已断开
#   2013 CR_SERVER_LOST            查询途中连接丢失("Lost connection ... during query")
#   2014 CR_COMMANDS_OUT_OF_SYNC   上一条语句的结果没读完就又发命令 —— 并发共用
#                                  一条连接的典型后果
#   2055 CR_SERVER_LOST_EXTENDED
_TRANSIENT_CONN_ERRNOS = frozenset({2006, 2013, 2014, 2055})

#: 无错误码时按 **类型名** 判「连接不可信」的异常(驱动/事件循环的命名差异)。
#: ``OperationalError`` **刻意不在其中** —— 它同时承载 1045(权限)、1205(锁等待)、
#: 1146(缺表)等不该拆连接的错误,只能按错误码判(见上面的常量)。
_TRANSIENT_CONN_TYPES = frozenset({
    "InterfaceError",        # pymysql 协议级:失步 / 对已关闭连接发命令 (0, '')
    "IncompleteReadError",   # asyncio 流读到一半 EOF(片段 / 服务端掐断)
    "ConnectionResetError",
    "ConnectionAbortedError",
    "BrokenPipeError",
})

#: 错误码与类型名都缺时的文本兜底(asyncio 流层 / OS 层 / 本适配器的包装)。
#: 只收「连接已不可信」的措辞:语法、权限、缺表一律不在其中 —— 它们重试
#: 无意义,拆连接更是纯浪费。
_TRANSIENT_CONN_MARKERS = (
    "lost connection",           # MySQL 原文: Lost connection to MySQL server ...
    "connection lost",           # 本适配器 _ping_reconnect 的包装措辞
    "server has gone away",      # MySQL 2006
    "commands out of sync",      # MySQL 2014
    "connection reset",
    "connection refused",
    "connection aborted",
    "connection closed",
    "broken pipe",
    "incomplete read",           # asyncio.IncompleteReadError 的报文
    "unexpected eof",
    # asyncio.StreamReader 的并发读签名原话(``read()`` / ``readexactly()`` 共用
    # 同一句):「两个协程在等同一个流的下一份数据」。这正是本适配器要根治的
    # 那条报文 —— 修好锁之后它不该再出现,判定留着是兜底(比如外部绕过适配器).
    "while another coroutine is already waiting",
)

#: 出错后**可以重发**的语句头(只读,幂等)。刻意不含 ``WITH``:MySQL 8 里
#: ``WITH`` 也能冠在 ``UPDATE`` / ``DELETE`` 前面(CTE + DML),同一个前缀
#: 既可能是读也可能是写 —— 而"能不能重发"完全取决于这一点。
_READ_STATEMENT_HEADS = frozenset({"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"})


def _error_code(exc: BaseException) -> int | None:
    args = getattr(exc, "args", None) or ()
    return args[0] if args and isinstance(args[0], int) else None


def _is_connection_lost(exc: BaseException | None) -> bool:
    """这条异常是否说明「当前连接已不可信」(收窄判定,依据见上方常量)。

    沿 ``__cause__`` 走一段:``_ping_reconnect`` / Doris 的同名钩子都把驱动
    异常包进了 ``DatasourceError(...) from e``,只看最外层会把原码漏掉。
    """
    seen = 0
    while exc is not None and seen < 4:
        if _error_code(exc) in _TRANSIENT_CONN_ERRNOS:
            return True
        if type(exc).__name__ in _TRANSIENT_CONN_TYPES:
            return True
        if any(m in str(exc).lower() for m in _TRANSIENT_CONN_MARKERS):
            return True
        exc = exc.__cause__
        seen += 1
    return False


def _is_retryable_read(sql: str) -> bool:
    """语句本身是否幂等可重发(只读)。判据与理由见 ``_READ_STATEMENT_HEADS``。"""
    text = str(sql or "").lstrip()
    # 跳过前导注释:``/* hint */ SELECT ...`` 与 ``-- note\\nSELECT ...`` 都是
    # 读语句。注释没有闭合就不判读 —— 连语句头都取不到时按不可重试处理。
    while text:
        if text.startswith("/*"):
            end = text.find("*/", 2)
            if end < 0:
                return False
            text = text[end + 2:].lstrip()
        elif text.startswith("--") or text.startswith("#"):
            end = text.find("\n")
            if end < 0:
                return False
            text = text[end + 1:].lstrip()
        else:
            break
    head = text.split(None, 1)[0].upper() if text else ""
    if head not in _READ_STATEMENT_HEADS:
        return False
    # ``SELECT ... INTO OUTFILE/DUMPFILE`` 会写文件 —— 读语句的外形,写的副作用。
    upper = text.upper()
    return "INTO OUTFILE" not in upper and "INTO DUMPFILE" not in upper


def _mysql_write_grants(grant_lines: list[str]) -> list[str]:
    """从 ``SHOW GRANTS`` 的行里挑出带写权限的那几条(整行返回,便于留依据)。"""
    hits = []
    for line in grant_lines:
        upper = str(line).upper()
        privileges, _, tail = upper.partition(" ON ")
        names = {
            p.strip()
            for p in privileges.removeprefix("GRANT ").split(",")
            if p.strip()
        }
        # ``WITH GRANT OPTION`` 的账号能**自己把 INSERT 授给自己**:今天没写权限
        # 不代表明天没有,所以它不是一道只读边界(它在 `` ON `` 之后,单独看)。
        if names & _MYSQL_WRITE_PRIVILEGES or "WITH GRANT OPTION" in tail:
            hits.append(str(line))
    return hits


class MySQLAdapter(DatabaseAdapter):
    """MySQL database adapter via aiomysql (async).

    Subclass overrides (Doris speaks the MySQL wire protocol):
      - ``label``          product name in connect/ping error messages
      - ``default_port``   server port when the config omits one
      - ``driver_hint``    pip/uv extra hint when aiomysql is missing
      - ``dialect()``      SQLGlot dialect
    """

    label = "MySQL"
    default_port = DEFAULT_PORT
    driver_hint = "`uv sync --extra mysql`"

    # get_schema 取 information_schema.TABLES 的 row_count / DATA_LENGTH /
    # UPDATE_TIME。三者都有文档写明的失效方式 —— 行数在统计未收集时是 NULL、
    # 字节数只算聚簇索引、时间戳只对非分区 InnoDB 表给值且重启后不持久 ——
    # 所以一律经 positive_int / timestamp_str 归一:不可得就是 None。
    profile_capabilities = frozenset({"row_count", "bytes", "last_modified"})

    #: 画像的额外列:(SQL 列名, 字段名, 取值归一函数)。
    #: **声明与实现同源** —— ``table_profiles`` 从这个元组生成 SELECT 列清单。
    #: 继承者若给不出这些列(Doris 的 information_schema 是 FE 虚拟表,这两列
    #: 的值无从验证),把它清空即可,连查都不会去查 —— 免得能力矩阵说没有、
    #: 实现照样去查、值再流进 data_as_of。
    _PROFILE_COLS: tuple[tuple[str, str, Callable[[Any], Any]], ...] = (
        ("DATA_LENGTH", "bytes", positive_int),
        ("UPDATE_TIME", "last_modified", timestamp_str),
    )

    # ── 并发:一条连接,多个协程 ────────────────────────────
    #
    # 一个适配器就是一个**共享单例**(registry 里按名字取同一个对象),而
    # agent 循环会**并行派发工具**(``agent_loop`` 的 ``asyncio.gather``):
    # probe_query / check_result 同轮落到同一个适配器上。两个协程等同一个
    # socket 的下一份数据,MySQL 协议就此失步 —— 症状是
    # ``(2013, 'Lost connection to MySQL server during query')`` 或 asyncio
    # 自己的 ``readexactly() called while another coroutine is already
    # waiting for incoming data``,而不是一条业务错误。
    #
    # 修法**不是连接池**:池把「共享一条连接」换成「共享一个池的记账」,而
    # 这里要的只是「同一时刻只有一个协程用这条连接」。``_conn_lock`` 串行化
    # 所有触碰 ``self._conn`` 的段;每个公共入口持锁后调 ``_xxx_locked`` /
    # 私有实现,私有实现里**不再取锁**(asyncio.Lock 不可重入,嵌套必自锁死)。
    #
    # 唯一的例外是 ``interrupt`` / ``_kill_query``:**不取锁**。它由 ``execute``
    # 的取消栈在**持锁状态**下调用(去杀自己那条查询),再取一次锁就是自锁死;
    # 它只读 ``thread_id``(纯属性,不是线上操作)并走**旁路连接**发 KILL
    # QUERY —— 本来就碰不到共享连接。
    #
    # 锁只保证不重叠;连接**断/失步之后**的恢复另有有界重试,见
    # ``_ensure_connected`` 与 ``_execute_locked``。

    def __init__(self, name: str = "mysql", config: dict[str, Any] | None = None):
        super().__init__(name, config or {})
        self._conn: Any = None
        self._server_version = ""
        self._conn_lock = asyncio.Lock()

    @classmethod
    def _get_driver(cls):
        """Import aiomysql lazily (raises DatasourceError with a hint when missing)."""
        try:
            import aiomysql
            return aiomysql
        except ImportError as e:
            raise DatasourceError(
                message=f"aiomysql is not installed — run {cls.driver_hint}",
                datasource="",
            ) from e

    @staticmethod
    def dialect() -> str:
        return "mysql"

    async def connect(self) -> None:
        async with self._conn_lock:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        """建连(**调用方必须已持有 ``_conn_lock``**)。

        守卫看的是「已有一条活连接」,不是 ``_connected`` 标志:连接被丢弃过
        (``_conn is None`` 但 ``_connected`` 仍为 True,见 ``_drop_connection``)
        时,这里的 connect 负责把它建回来。
        """
        if self._conn is not None and self._connected:
            return
        try:
            aiomysql = self._get_driver()
            self._conn = await aiomysql.connect(
                host=self.config.get("host", "127.0.0.1"),
                port=self.config.get("port", self.default_port),
                user=self.config.get("user", ""),
                password=self.config.get("password", ""),
                db=self.config.get("database", ""),
            )
            self._connected = True
            # 先探测版本,再装库侧兜底闸(见 supports_statement_timeout):
            # MySQL 与 MariaDB 的超时变量**不同**(单位也不同),装闸要看着
            # 引擎选;版本探测是服务端常量查询、不扫数据,先跑它的暴露可以忽略。
            await self._probe_version()
            await self._statement_timeout_locked()   # 锁已在本方法调用方手里
            logger.debug("Connected to %s: %s:%s/%s",
                         self.label, self.config.get("host"), self.config.get("port"),
                         self.config.get("database"))
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"Failed to connect to {self.label} at "
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
        # 关连接也走锁:否则会在一条正在跑的语句下方把 socket 抽掉,下一条
        # 语句拿到的是半条连接。等锁的时间被在飞语句自己的超时界住。
        async with self._conn_lock:
            self._disconnect_locked()

    def _disconnect_locked(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
        self._connected = False

    def _drop_connection(self) -> None:
        """丢弃当前连接:**关闭、不复用**(失步的连接状态不可判)。

        ``_connected`` 保持 True —— 适配器仍处「应已连接」状态,下一次使用
        会重建(见 ``_ensure_connected``);只有显式 ``disconnect`` 才把它
        置 False(那之后 execute 必须快速失败,不许隐式建连)。
        """
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception as e:   # 关一条已坏的连接失败,不改变"它已不可用"
                logger.debug("%s dropping connection: close failed: %s", self.label, e)

    async def _rebuild_connection(self) -> None:
        """丢弃旧连接并建一条新的;建不起来如实抛 ``reconnect failed``。"""
        self._drop_connection()
        try:
            await self._connect_locked()
        except Exception as e:
            raise DatasourceError(
                message=f"{self.label} connection lost and reconnect failed: {e}",
                datasource=self.name,
            ) from e

    supports_interrupt = True

    # DB 侧语句超时(§10):会话变量,只对只读 SELECT 生效。变量名与单位
    # **随引擎不同**(MySQL ``max_execution_time``/毫秒,MariaDB
    # ``max_statement_time``/秒),由 ``_statement_timeout_sql`` 归一。
    supports_statement_timeout = True

    #: MariaDB 的版本串一定带这个词(``10.6.12-MariaDB-1:...``;
    #: 旧客户端协议下是 ``5.5.5-10.x.y-MariaDB``,同样命中)。
    MARIADB_MARK = "mariadb"

    def _statement_timeout_sql(self, ms: int) -> str:
        """按引擎选超时语句。

        **MariaDB 不认 ``max_execution_time``**(MySQL 独有,发了直接报
        Unknown system variable);它自己的叫 ``max_statement_time``,而且
        单位是**秒**(10.1.1+,双精度,精度 1ms)。引擎从版本探测的字符串
        判;探测失败(空串)按 MySQL 处理 —— 猜错的代价是一条 WARNING
        (被拒 → 降级记录),而不是「用错变量还显示已装闸」。
        """
        if self.MARIADB_MARK in (self._server_version or "").lower():
            return f"SET SESSION max_statement_time = {ms / 1000.0:g}"
        return f"SET SESSION max_execution_time = {int(ms)}"

    async def apply_statement_timeout(self) -> bool:
        """公共入口(基类策略 + 本类钩子):与其余触碰 ``self._conn`` 的段一样**持锁**。

        基类实现最后会在这条共享连接上发一条 ``SET SESSION``,所以它同样
        是「线上操作」,不能游离在锁外。锁内路径(``_connect_locked`` /
        ``_ping_reconnect``)改调 ``_statement_timeout_locked`` —— 同一个
        实现,只是不再取一次锁(asyncio.Lock 不可重入,重入必自锁死)。
        """
        async with self._conn_lock:
            return await self._statement_timeout_locked()

    async def _statement_timeout_locked(self) -> bool:
        """基类策略原样(能力声明 → ms 解析 → 本类钩子);**调用方必须已持锁**。

        直接调基类实现(``super()``)而不是 ``self.``:后者会回到本类的
        公共入口,在锁内再取一次锁 = 死锁。
        """
        return await super().apply_statement_timeout()

    async def _apply_statement_timeout(self, ms: int) -> bool:
        """``SET SESSION <超时变量>``(见 ``_statement_timeout_sql``)。一条龙 try —— 永不抛。

        这条语句是**兜底闸**,而它对面是另一个事实:MySQL 协议的**其他引擎**
        (MySQL < 5.7.8、MariaDB < 10.1.1、以及 Doris 这类语义未验证的实现)
        未必认这条语句。在旧引擎上让整条连接失败,是用一次可用的降级换一次
        不可用的保护;但降级必须看得见 —— 警告里点名语句与后果,返回值
        False 供调用方记录。
        """
        if self._conn is None:
            return False
        statement = self._statement_timeout_sql(ms)
        try:
            cursor = await self._conn.cursor()
            try:
                await cursor.execute(statement)
            finally:
                await cursor.close()
        except Exception as e:
            logger.warning(
                "%s rejected %r (%s: %s) — DB-side statement timeout is NOT in "
                "effect on this connection (MySQL < 5.7.8 / MariaDB < 10.1.1 / "
                "other MySQL-protocol engines); the app-side budget remains the "
                "primary gate", self.label, statement, type(e).__name__, e,
            )
            return False
        logger.debug("%s statement timeout armed: %s", self.label, statement)
        return True

    async def interrupt(self) -> bool:
        """KILL QUERY via a side connection (a busy connection can't serve it).

        Best-effort and bounded: thread-id lookup may be sync or coroutine
        across aiomysql versions, and any failure just logs at warning —
        the cancellation unwind must never hang.

        返回 False 的两种情形在调用方看来是同一件事(**终止没能发出**):
        旁路连接建不起来、``KILL QUERY`` 被执行被拒。上游据此记
        ``kill_failed`` 并 WARN,原因看日志。

        「查不到 thread id / 没连接」**不算失败**(见 ``_kill_query``):那是
        「本就无可取消」,与基类契约一致。

        **刻意不取 ``_conn_lock``**:调用方是 ``execute`` 的取消栈,那一刻锁
        就在它手里 —— 再取一次就是自锁死。它读 ``thread_id``(纯属性,不是
        线上操作)、走旁路连接发 KILL,与共享连接的线上状态无关。
        """
        try:
            await asyncio.wait_for(self._kill_query(), timeout=INTERRUPT_TIMEOUT_S)
        except Exception as e:
            logger.warning("MySQL interrupt failed: %s", e)
            return False
        return True

    async def _kill_query(self) -> None:
        """发 KILL QUERY;发不出去就 raise,由 ``interrupt`` 折成 False。

        **「没有在飞的查询」不算失败**(与基类契约一致):查不到 thread id /
        没连接时直接返回,那是「本就无可取消」。真正的失败往下抛。
        """
        if self._conn is None:
            return
        getter = getattr(self._conn, "thread_id", None)
        if getter is None:
            return
        try:
            tid = getter() if not asyncio.iscoroutinefunction(getter) else await getter()
        except Exception:
            return
        if tid is None:
            return
        aiomysql = self._get_driver()
        side = await aiomysql.connect(
            host=self.config.get("host", "127.0.0.1"),
            port=self.config.get("port", self.default_port),
            user=self.config.get("user", ""),
            password=self.config.get("password", ""),
            db=self.config.get("database", ""),
        )
        try:
            cursor = await side.cursor()
            try:
                await cursor.execute(f"KILL QUERY {int(tid)}")
            finally:
                await cursor.close()
        finally:
            side.close()

    async def probe_readonly(self) -> ReadonlyProbe:
        """``SHOW GRANTS``(设计 §4 I1)。

        **只查权限表,绝不试写**。设计稿给的另一条路是「尝试一条必然失败的写
        语句」—— 它「必然失败」的前提正是「账号只读」这个待证假设;账号其实可写
        时,那条语句会真的写进去,一个探测变成一次生产写入。

        用不带 ``FOR`` 的 ``SHOW GRANTS``:它就是这个账号自己的授权,而且是
        MySQL 与 Doris 都认的形式(Doris 的 ``information_schema`` 是 FE 虚拟表,
        不代表真实权限;``SHOW GRANTS`` 才是它的权威源)。

        不接异常:查询被拒/连接断了,**如实往上抛**,由 ``readonly.probe`` 折成
        ``probe_failed``。在这里吞掉它就等于让「没查成」消失在实现里。
        """
        async with self._conn_lock:
            return await self._probe_readonly_locked()

    async def _probe_readonly_locked(self) -> ReadonlyProbe:
        await self._ensure_connected()
        cursor = await self._conn.cursor()
        try:
            await cursor.execute("SHOW GRANTS")
            rows = await cursor.fetchall()
        finally:
            await cursor.close()

        grants = [str(r[0]) for r in rows if r and r[0] is not None]
        if not grants:
            # 一条都没看到 ≠ 什么都不能干:更可能是这条路径没拿到东西。往 True
            # 倒就是替一道并不存在的边界背书,而这正是 I1 要防的那个谎。
            return ReadonlyProbe(
                None, BASIS_PROBE_FAILED, "SHOW GRANTS returned no rows",
            )
        writes = _mysql_write_grants(grants)
        if writes:
            return ReadonlyProbe(False, BASIS_GRANTS, "; ".join(writes))
        return ReadonlyProbe(
            True, BASIS_GRANTS,
            f"{len(grants)} grant line(s), none of them a write privilege",
        )

    async def _ping_reconnect(self) -> None:
        """Reconnect if the underlying connection went stale.

        MySQL closes idle connections (wait_timeout); a long-running
        `trove serve` then turns every query/catalog call into a raw
        driver exception (InterfaceError/OperationalError). ping with
        reconnect transparently reopens the connection when the server
        is reachable again.

        **调用方必须已持有 ``_conn_lock``**(连接生命周期内部钩子);失败由
        ``_ensure_connected`` 接手(丢弃 + 重建一次)。
        Doris 覆写本方法(FE 不认 COM_PING,改用 ``SELECT 1``)—— 覆写体同样
        只允许在锁内被调用。
        """
        try:
            await self._conn.ping(reconnect=True)
        except Exception as e:
            raise DatasourceError(
                message=f"{self.label} connection lost and reconnect failed: {e}",
                datasource=self.name,
            ) from e
        # 重连(若发生)会带走会话变量,而 ping 是否真重连**无法从返回值区分**
        # (aiomysql 原地换 socket,Connection 对象不变)—— 与其猜,不如每次
        # ping 后都重发一次:一条廉价 SET,换掉「以为有界其实没有」这一个
        # 失败模式。未声明/显式关闭的方言在这一行里自然什么都不发。
        await self._statement_timeout_locked()   # 锁已在本方法调用方手里

    async def _ensure_connected(self) -> None:
        """确保当前有一条可用连接(**调用方必须已持有 ``_conn_lock``**)。

        三种状态:
          * 从未连接 / 已显式 disconnect → ``Not connected``(不隐式建连);
          * 连接被丢弃过(``_conn is None`` 但 ``_connected``)→ 重建后即用;
          * 有连接 → ping(``reconnect=True`` 覆盖服务端 wait_timeout 掐断)。

        ping 失败只有一种解释:这条连接已经不工作(ping 自带的重连也救不回
        一条失步的连接)—— 此时**语句还没跑**,重建一次对任何语句都安全
        (重试的是连接,不是语句),所以这里不做读/写区分。重建仍有界:
        建不起来就抛 ``reconnect failed``,由调用方如实处理。
        """
        if self._conn is None:
            if not self._connected:
                raise DatasourceError(message="Not connected", datasource=self.name)
            await self._rebuild_connection()
        try:
            await self._ping_reconnect()
        except Exception as e:
            logger.warning(
                "%s connection unusable (%s: %s); rebuilding it before running "
                "the statement", self.label, type(e).__name__, e,
            )
            await self._rebuild_connection()

    async def execute(self, sql: str) -> QueryResult:
        async with self._conn_lock:
            return await self._execute_locked(sql)

    async def _execute_locked(self, sql: str) -> QueryResult:
        if not self._conn and not self._connected:
            # 从未连接 / 已显式 disconnect:与历史一致地快速失败(不隐式建连)。
            raise SQLExecutionError(message="Not connected to MySQL", sql=sql)

        # 连接层:ping 失败会在这里被重建一次(语句尚未执行,任何语句都安全)。
        await self._ensure_connected()

        try:
            return await self._run_query(sql)
        except Exception as e:
            if not _is_connection_lost(e):
                raise self._execution_error(sql, e) from e
            # 连接已不可信:**一律丢弃**(不复用),但**只有读语句重试** ——
            # 这条连接上「语句到底执行了没有」不可判,写语句重发可能产生
            # 第二次副作用,读语句重发是幂等的。丢弃不等于重发:写语句丢掉
            # 连接后直接把错误抛出去,由下一次调用用新连接。
            if not _is_retryable_read(sql):
                self._drop_connection()
                raise self._execution_error(sql, e) from e
            logger.warning(
                "%s lost the connection while running a read statement (%s: %s); "
                "discarding it and retrying once on a fresh connection",
                self.label, type(e).__name__, e,
            )
            await self._rebuild_connection()
            try:
                return await self._run_query(sql)
            except Exception as e2:
                if _is_connection_lost(e2):
                    self._drop_connection()   # 新连接也失步:别留给下一条语句
                raise self._execution_error(sql, e2) from e2

    async def _run_query(self, sql: str) -> QueryResult:
        """在**当前**连接上跑一条语句(调用方已持锁、已确保连接可用)。"""
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
        except asyncio.CancelledError:
            # 客户端中止:在跑连接发不了 KILL,走旁路连接 KILL QUERY
            # (同用户可杀自己的查询),服务端真正停止执行。
            #
            # **顺序要紧**:先 interrupt 再关游标。MySQL 游标的 close 会去
            # 抽干剩余结果,在一条失步/卡住的 socket 上这一步可能一直等 ——
            # 先让服务端停下这条查询,那次读才会快点失败。
            await self.interrupt()
            raise
        finally:
            await cursor.close()

    def _execution_error(self, sql: str, e: BaseException) -> SQLExecutionError:
        return SQLExecutionError(
            message=f"MySQL execution error: {e}",
            sql=sql,
            db_error=str(e),
        )

    async def get_schema(self) -> SchemaInfo:
        async with self._conn_lock:
            return await self._get_schema_locked()

    async def _get_schema_locked(self) -> SchemaInfo:
        await self._ensure_connected()

        tables = []
        cursor = await self._conn.cursor()
        try:
            await cursor.execute(
                "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
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
                    # 原样带出 NULL —— 「统计缺失」与「空表」必须是两个值
                    row_count_estimate=positive_int(row_count),
                ))
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"MySQL schema introspection failed: {e}",
                datasource=self.name,
            ) from e
        finally:
            await cursor.close()

        return SchemaInfo(tables=tables)

    async def table_profiles(self) -> dict[str, TableProfile]:
        """画像:行数 + 表字节数 + 最近写入时间(§8.2 B 的方言实现)。

        一次 ``information_schema.TABLES`` 拿全。**不与 ``get_schema`` 合并**:
        列信息是每表一次 ``COLUMNS`` 查询,画像要的三个量一次就够,合并等于每次
        刷新都付一遍架构内省的钱(§10)。

        ``UPDATE_TIME`` 取到值时是**下界**(I_S 的统计列默认缓存 24h,change
        buffer 又会让它偏旧),方向安全:宁可说「数据截至更早」,不可说更新。
        """
        async with self._conn_lock:
            return await self._table_profiles_locked()

    async def _table_profiles_locked(self) -> dict[str, TableProfile]:
        await self._ensure_connected()
        caps = self.profile_capabilities
        cols = ["TABLE_NAME", "TABLE_ROWS", *(c for c, _, _ in self._PROFILE_COLS)]

        cursor = await self._conn.cursor()
        try:
            await cursor.execute(
                f"SELECT {', '.join(cols)} FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
            )
            rows = await cursor.fetchall()
        except Exception as e:
            raise DatasourceError(
                message=f"{self.label} profile introspection failed: {e}",
                datasource=self.name,
            ) from e
        finally:
            await cursor.close()

        out: dict[str, TableProfile] = {}
        for row in rows:
            name, table_rows, *extras = row
            fields: dict[str, Any] = {"row_count": positive_int(table_rows)}
            for (_, field, coerce), raw in zip(self._PROFILE_COLS, extras):
                fields[field] = coerce(raw)
            out[str(name)] = TableProfile(table=str(name), capabilities=caps, **fields)
        return out

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
