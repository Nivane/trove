"""MySQL database adapter (aiomysql, async).

Introspection via information_schema:
  - tables: TABLE_NAME + TABLE_ROWS (approximate for InnoDB, fine for estimates)
  - columns: COLUMN_NAME / DATA_TYPE / IS_NULLABLE / COLUMN_KEY ('PRI' = PK)

The driver is imported lazily so the adapter module stays importable
without `uv sync --extra mysql`.
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

    def __init__(self, name: str = "mysql", config: dict[str, Any] | None = None):
        super().__init__(name, config or {})
        self._conn: Any = None
        self._server_version = ""

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
        if self._connected:
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
            await self.apply_statement_timeout()
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
        if self._conn:
            self._conn.close()
            self._conn = None
        self._connected = False

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
        await self.apply_statement_timeout()

    async def _ensure_connected(self) -> None:
        """Ensure a live connection, reconnecting a stale one."""
        if not self._conn or not self._connected:
            raise DatasourceError(message="Not connected", datasource=self.name)
        await self._ping_reconnect()

    async def execute(self, sql: str) -> QueryResult:
        if not self._conn or not self._connected:
            raise SQLExecutionError(message="Not connected to MySQL", sql=sql)
        await self._ping_reconnect()

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
            await self.interrupt()
            raise
        except Exception as e:
            raise SQLExecutionError(
                message=f"MySQL execution error: {e}",
                sql=sql,
                db_error=str(e),
            ) from e
        finally:
            await cursor.close()

    async def get_schema(self) -> SchemaInfo:
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
