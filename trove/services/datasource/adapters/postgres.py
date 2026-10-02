"""PostgreSQL database adapter (psycopg async).

Introspection via information_schema + pg_catalog:
  - tables: pg_class/pg_namespace with reltuples (approximate row counts)
  - columns: information_schema.columns + primary-key join

The driver is imported lazily so the adapter module stays importable
without `uv sync --extra postgres`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

from trove.core.types import (
    BASIS_GRANTS,
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

DEFAULT_PORT = 5432


def _get_driver():
    """Import psycopg lazily (raises DatasourceError with a hint when missing)."""
    try:
        import psycopg
        return psycopg
    except ImportError as e:
        raise DatasourceError(
            message="psycopg is not installed — run `uv sync --extra postgres`",
            datasource="",
        ) from e


def _conninfo(config: dict[str, Any], credentials: dict[str, str] | None = None) -> str:
    """Build a psycopg conninfo string from connection params + credentials."""
    params = {**config, **(credentials or {})}
    host = params.get("host", "127.0.0.1")
    port = params.get("port", DEFAULT_PORT)
    user = params.get("user", "")
    password = params.get("password", "")
    database = params.get("database", "")
    auth = ""
    if user and password:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    elif user:
        auth = f"{quote(user, safe='')}@"
    elif password:
        auth = f":{quote(password, safe='')}@"
    return f"postgresql://{auth}{host}:{port}/{database}"


# ── 只读自检(设计 §4 I1)────────────────────────────────
#
# 权限表怎么问,PG 和 MySQL 的**问题形状**不一样:``SHOW GRANTS`` 直接给出
# 「这个账号的授权原文」,PG 没有等价物 —— 权限散在 pg_roles(超管)、
# pg_class.relowner(属主)、ACL(授权)三处,只好一次问三个计数再合起来看。
#
# 来源的划分照设计 §4 I1,但**「直接授予当前角色」这一条不够**:下面两条是
# 从 PG 官方来源读出来的事实,不是猜的 ——
#
# * ``information_schema.table_privileges`` **含 PUBLIC 授予**:
#   ``GRANT ... TO PUBLIC`` 那一行的 ``grantee`` 是字面量 ``'PUBLIC'``
#   (视图定义里 UNION 了一个 ``SELECT 0::oid, 'PUBLIC'`` 的合成行,WHERE 里
#   另有一条 ``grantee.rolname = 'PUBLIC'`` 的显式分句)。它**不等于**
#   ``current_user``,只按 ``grantee = current_user`` 过滤就问不到它。
# * 它**不展开角色继承**:每一行对应 ``aclexplode(relacl)`` 里的一条 ACL,
#   授予我所属角色的权限,行上写的是**那个角色的名字**。视图 WHERE 里的
#   ``pg_has_role(...)`` 说的是「这一行对我可见吗」,**不是**「我有这个权限
#   吗」。于是 ``GRANT SELECT ... TO ro_group; GRANT ro_group TO app_user``
#   ——最常见的那种只读账号配法——会被问成「0 写权限」。
#
# 两条漏的都是 **False → True** 的方向(把写得动的账号报成只读),而 ``True``
# 正是替那道边界背书。所以这里的 ``grantee`` 判据不是 ``= current_user``,
# 而是「PUBLIC 或我可达的角色」。
#
# 出处:视图定义见 PG 源码 src/backend/catalog/information_schema.sql;
#   https://www.postgresql.org/docs/current/infoschema-table-privileges.html
#   https://www.postgresql.org/docs/current/functions-info.html(pg_has_role)

#: 「我可达的角色」—— 我自己,加上我(直接或间接)是成员的角色。
#:
#: 用 ``MEMBER`` 而不是 ``USAGE``:``USAGE`` 问「这个角色的权限**当场**可用
#: 吗」(``NOINHERIT`` 的成员关系答否),``MEMBER`` 问「我能不能成为它」。
#: 两者差的正是 ``SET ROLE`` —— 能 ``SET ROLE`` 到写角色上的账号不是一道只读
#: 边界。取宽的那个,方向落在**安全侧**:多报一次 ``False`` 只是启动时多一条
#: WARN,少报一次就是把可写的账号说成只读。
#:
#: ``rolname = current_user`` 单独写出来,不省进 ``pg_has_role``:文档没有说
#: 「自己是自己的成员」,不拿一条没写明的语义当依据。
#:
#: 用 ``current_user`` 而不是 ``session_user``:探测问的是**这条连接现在能
#: 做什么**,``SET ROLE`` 之后生效的是前者。
#:
#: 两个片段是同一个集合的两种键(oid 给目录列,rolname 给授权表),所以都在。
_PG_ACTABLE_ROLE_OIDS = (
    "SELECT oid FROM pg_roles "
    "WHERE rolname = current_user OR pg_has_role(oid, 'MEMBER')"
)
_PG_ACTABLE_ROLE_NAMES = (
    "SELECT rolname FROM pg_roles "
    "WHERE rolname = current_user OR pg_has_role(oid, 'MEMBER')"
)

#: ``table_privileges`` 里算「写得动」的权限名(它的取值域就这七个:
#: SELECT / INSERT / UPDATE / DELETE / TRUNCATE / REFERENCES / TRIGGER)。
#: ``TRUNCATE`` 收进来:它不改数据,但一把清空绝不是只读边界该有的东西,
#: 而 I1 断言的正是「这道边界存在」。
_PG_TABLE_WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")

#: 列级写得动的只有这两个 —— ``column_privileges`` 的取值域是
#: SELECT / INSERT / UPDATE / REFERENCES,列级没有 DELETE / TRUNCATE。
_PG_COLUMN_WRITE_PRIVILEGES = ("INSERT", "UPDATE")

#: 授权行的判据,**表级与列级两处共用**(写成一句,免得两边漂移)。
#:
#: PUBLIC 只能按名字单独判:官方文档明说 ``pg_has_role`` 不接受 ``public``
#: (「the PUBLIC pseudo-role can never be a member of real roles」),所以这
#: 两条判据合不成一句 ``IN (...)``。
_PG_WRITE_GRANT_REACHES_ME = (
    f"grantee = 'PUBLIC' OR grantee IN ({_PG_ACTABLE_ROLE_NAMES})"
)


def _sql_str_list(values: tuple[str, ...]) -> str:
    """把权限名拼成 SQL 串列表(取值都是本模块的字面量,不是外部输入)。"""
    return ", ".join(f"'{v}'" for v in values)


#: 那条查询:**一条 SELECT,三列,一行**。列名即判据,顺序即 ``_pg_counts``
#: 的解包顺序。
#:
#: 三列各自的理由:
#:
#: * ``is_super``  超管**独立于 ACL**:一条授权都没授,照样能写。可达的超管
#:   角色(含继承来的)也算 —— ``SET ROLE`` 上去一样是超管。
#: * ``owned``     属主天然可写,与 GRANT 无关。``relkind`` 取
#:   ``('r','p','m','f')``:普通表、分区表、**物化视图**(属主能 REFRESH,
#:   那是写)、外部表。注意 ``table_privileges`` 自己的 relkind 过滤里**没有**
#:   ``'m'``,所以物化视图只能靠这一列兜住。
#: * ``writes``    表级写权限 **加上**列级写权限。列级那些不在
#:   ``table_privileges`` 里(它只展开 ``pg_class.relacl``,列 ACL 在
#:   ``pg_attribute.attacl``、由 ``column_privileges`` 展开),``GRANT
#:   UPDATE(col)`` 是一条实实在在的写路径,漏掉它就等于漏掉一整类账号。
#:   两臂用 ``UNION`` 而不是 ``UNION ALL``:表级授权在 ``column_privileges``
#:   里会按列各铺一行,不去重的话一个 INSERT 会被数成「N 条依据」。
_PG_READONLY_SQL = f"""
SELECT
  (SELECT count(*) FROM pg_roles
    WHERE rolsuper AND oid IN ({_PG_ACTABLE_ROLE_OIDS})) AS is_super,
  (SELECT count(*) FROM pg_class
    WHERE relkind IN ('r', 'p', 'm', 'f')
      AND relowner IN ({_PG_ACTABLE_ROLE_OIDS})) AS owned,
  (SELECT count(*) FROM (
      SELECT table_schema, table_name, privilege_type
        FROM information_schema.table_privileges
       WHERE ({_PG_WRITE_GRANT_REACHES_ME})
         AND privilege_type IN ({_sql_str_list(_PG_TABLE_WRITE_PRIVILEGES)})
      UNION
      SELECT table_schema, table_name, privilege_type
        FROM information_schema.column_privileges
       WHERE ({_PG_WRITE_GRANT_REACHES_ME})
         AND privilege_type IN ({_sql_str_list(_PG_COLUMN_WRITE_PRIVILEGES)})
    ) write_grants) AS writes
"""


def _pg_counts(rows: Any) -> tuple[int, int, int] | None:
    """把结果折成三个计数;形状不对就是 ``None``,由调用方抛出去。

    这条查询**结构上**只回一行三列,所以别的形状都说明问错了东西(或者驱动
    给了别的东西)。``bool`` 单独挡掉:``True`` 是 ``int`` 的子类,而
    ``count(*)`` 不可能返回它 —— 真出现了说明这一列不是计数,照着 ``int``
    用就会把一次「取不到」变成一条结论。
    """
    if not isinstance(rows, list) or len(rows) != 1:
        return None
    row = rows[0]
    if not isinstance(row, (list, tuple)) or len(row) != 3:
        return None
    counts: list[int] = []
    for value in row:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        counts.append(value)
    return counts[0], counts[1], counts[2]


def _pg_shape(rows: Any) -> str:
    """形状描述,**只说行列数、不说值**。

    这段文本会跟着 ``DatasourceError`` 进 ``readonly.probe`` 的 detail —— 与
    健康检查「错误只报类型名,不回传驱动原文」是同一条纪律:形状不对时那些值
    到底是什么,谁也说不准,而它可能正是从库里捞出来的数据。
    """
    if not isinstance(rows, (list, tuple)):
        return f"not a row list, but {type(rows).__name__}"
    widths = [len(r) if isinstance(r, (list, tuple)) else "?" for r in rows]
    return f"{len(rows)} row(s), column counts {widths}"


class PostgresAdapter(DatabaseAdapter):
    """PostgreSQL database adapter via psycopg (async)."""

    # 三处来源:pg_class.reltuples(行数)、pg_relation_size(字节)、
    # pg_stat_user_tables.last_analyze(统计收集时间)。
    # **没有 last_modified**:PG 的目录里没有「数据最后修改时间」这个字段,
    # 不拿 last_analyze 顶替 —— 那是「什么时候统计的」,不是「数据到哪儿了」。
    profile_capabilities = frozenset({"row_count", "bytes", "last_analyzed"})

    def __init__(self, name: str = "postgres", config: dict[str, Any] | None = None):
        super().__init__(name, config or {})
        self._conn: Any = None

    @staticmethod
    def dialect() -> str:
        return "postgres"

    async def connect(self) -> None:
        if self._connected:
            return
        try:
            psycopg = _get_driver()
            self._conn = await psycopg.AsyncConnection.connect(
                _conninfo(self.config, self.config.get("credentials")),
                **self.statement_timeout_connect_kwargs(),
            )
            self._connected = True
            logger.debug("Connected to PostgreSQL: %s:%s/%s",
                         self.config.get("host", "127.0.0.1"),
                         self.config.get("port", DEFAULT_PORT),
                         self.config.get("database", ""))
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=(
                    f"Failed to connect to PostgreSQL at "
                    f"{self.config.get('host', '127.0.0.1')}:"
                    f"{self.config.get('port', DEFAULT_PORT)}: {e}"
                ),
                datasource=self.name,
            ) from e

    async def disconnect(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
        self._connected = False

    supports_interrupt = True

    # DB 侧语句超时(§10):走连接参数 ``options=-c statement_timeout=<ms>``。
    # **不用连接后的 SET**:psycopg3 默认 ``autocommit=False``,那条 SET 会落进
    # 一个隐式事务,任何一次 rollback 都会把它一起带走 —— 一道「随时可能被
    # 回滚掉」的闸不是闸。``options`` 在握手时就把 GUC 交给服务端;每次
    # ``connect()``(含 ``_ensure_connected`` 对 stale 连接的重连)都重新带上,
    # 「重连丢会话变量」这个失败模式在参数形态下根本不存在。
    supports_statement_timeout = True

    def _statement_timeout_connect_kwargs(self, ms: int) -> dict[str, Any]:
        return {"options": f"-c statement_timeout={int(ms)}"}

    async def interrupt(self) -> bool:
        """psycopg AsyncConnection.cancel() — 服务端取消在跑查询(psycopg 3.1+)。

        与设计 §7.3 的 ``pg_cancel_backend(pid)`` 有一处**有意不同**:协议级的
        CancelRequest 直接打在**当前这条连接**的后端上,不需要先从
        ``pg_stat_activity`` 查出 pid 再拼一条 SQL —— 少一次往返,也少掉
        R4 里「打错 pid 杀错会话」的整类风险(那个 pid 是别人给的,这个是
        连接自己带走的)。
        """
        try:
            if self._conn is not None and hasattr(self._conn, "cancel"):
                await asyncio.wait_for(
                    self._conn.cancel(), timeout=INTERRUPT_TIMEOUT_S,
                )
        except Exception as e:
            logger.warning("PostgreSQL interrupt failed: %s", e)
            return False
        return True

    async def probe_readonly(self) -> ReadonlyProbe:
        """查权限表问清这个账号能不能写(设计 §4 I1)。

        **只查权限表,绝不试写**。设计稿给的另一条路是「尝试一条必然失败的写
        语句」—— 它「必然失败」的前提正是「账号只读」这个待证假设;账号其实
        可写时,那条语句会**真的写进去**:一个探测变成一次生产写入。

        PG 这边问三件事(见 ``_PG_READONLY_SQL``):是不是超管、是不是谁的
        属主、有没有写权限。**属主必须单独问** —— 属主天然可写,与 GRANT
        无关,只看授权表的实现会把一个只有属主身份的账号报成只读。

        判定:三者任一非零 → ``False``(写得动);三者全为 0 → ``True``。
        其余一律不算结论:查询报错、行数不对、列数不对、值不是计数,**如实
        往上抛**,由 ``readonly.probe`` 折成 ``probe_failed``。在这里吞掉它,
        就等于让「没查成」消失在实现里 —— 而它折出来的是个看得见的状态。

        这条查询**覆盖不到**什么,照实写在这里。它们都是「往 True 漏」的
        方向,所以宁可说清楚,也不用 detail 里一句大话盖过去:

        * 写路径只覆盖**现有关系**。``CREATE ON SCHEMA`` / ``CREATE ON
          DATABASE``(能建新表)、``TEMPORARY`` 不在内 —— 这句 ``True`` 说的是
          「对已有对象无写权限」,不是「绝对写不动」。
        * ``NOINHERIT`` 的成员关系。它能 ``SET ROLE`` 变成写角色,但
          ``table_privileges`` 自己的可见性分句用的判据是 USAGE(NOINHERIT
          答否),所以这一类**可能连授权行都看不到**,在这里无法确认。
        * 序列(``GRANT UPDATE ON SEQUENCE`` → ``setval``)这类 ``relkind``
          为 ``'S'`` 的对象:不是表写,不收,也不声明覆盖。
        * ``information_schema`` 只报**当前数据库**(``table_catalog`` 恒等于
          当前库),跨库的授权不在这一句里。
        """
        await self._ensure_connected()

        async with self._conn.cursor() as cur:
            await cur.execute(_PG_READONLY_SQL)
            rows = await cur.fetchall()

        counts = _pg_counts(rows)
        if counts is None:
            # 形状不对 = **取不到**。不当成 0:0 会一路走到 ``True``,而那是在
            # 替一道并不存在的边界背书。抛出去让编排器折成 ``probe_failed``。
            raise DatasourceError(
                message=(
                    "PostgreSQL readonly probe returned an unexpected shape: "
                    f"{_pg_shape(rows)}"
                ),
                datasource=self.name,
            )

        is_super, owned, writes = counts
        reasons = []
        if is_super:
            reasons.append(f"{is_super} superuser role(s) reachable")
        if owned:
            reasons.append(f"owns {owned} relation(s)")
        if writes:
            reasons.append(f"{writes} write privilege entr(ies) reaching this role")
        if reasons:
            # detail 的既定去处就是日志(``ReadonlyProbe`` 的注释):启动那条
            # 汇总行只报 basis,依据得在这里说全。
            logger.debug("PostgreSQL readonly probe: %s", "; ".join(reasons))
            return ReadonlyProbe(False, BASIS_GRANTS, "; ".join(reasons))

        return ReadonlyProbe(
            True, BASIS_GRANTS,
            "no superuser, owns no relation, and no INSERT/UPDATE/DELETE/TRUNCATE "
            "grant reaches this role (direct, inherited or PUBLIC); "
            "schema-level CREATE and NOINHERIT memberships not covered",
        )

    async def _ensure_connected(self) -> None:
        """Ensure a live connection, transparently reconnecting a stale one.

        Postgres closes idle connections; a long-running `trove serve`
        must not turn every query into a raw driver exception. A closed
        connection is reopened via :meth:`connect` (reset the connected
        flag first so connect()'s guard doesn't short-circuit).
        """
        if not self._conn or not self._connected:
            raise DatasourceError(message="Not connected", datasource=self.name)
        if self._conn.closed:
            logger.info("PostgreSQL connection stale; reconnecting")
            self._conn = None
            self._connected = False
            await self.connect()

    async def execute(self, sql: str) -> QueryResult:
        if not self._conn or not self._connected:
            raise SQLExecutionError(message="Not connected to PostgreSQL", sql=sql)
        await self._ensure_connected()

        start = time.monotonic()
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(sql)
                # 不产记录的语句(CREATE/INSERT/UPDATE)在 psycopg3 里
                # fetchall() 会抛 ProgrammingError,而 MySQL/SQLite 的驱动
                # 只回空列表 —— description 为 None 就是「这条语句没有结果集」,
                # 按同一个口径回空列空行,别让同一个 QueryResult 契约因方言而异。
                rows = await cur.fetchall() if cur.description else []
                columns = [d.name for d in (cur.description or [])]
                elapsed_ms = (time.monotonic() - start) * 1000
        except asyncio.CancelledError:
            # 客户端中止:psycopg cancel() 是服务端协议级取消(另开通道),
            # 正在跑的查询会真正停下。
            await self.interrupt()
            raise
        except Exception as e:
            raise SQLExecutionError(
                message=f"PostgreSQL execution error: {e}",
                sql=sql,
                db_error=str(e),
            ) from e

        return QueryResult(
            columns=columns,
            rows=[list(row) for row in rows],
            row_count=len(rows),
            execution_time_ms=round(elapsed_ms, 2),
            sql=sql,
            datasource=self.name,
        )

    async def get_schema(self) -> SchemaInfo:
        await self._ensure_connected()

        tables = []
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    "SELECT t.relname, t.reltuples::bigint AS row_count "
                    "FROM pg_class t "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "WHERE n.nspname = current_schema() AND t.relkind = 'r' "
                    "ORDER BY t.relname"
                )
                table_rows = await cur.fetchall()

                for tname, row_count in table_rows:
                    await cur.execute(
                        "SELECT c.column_name, c.data_type, c.is_nullable, "
                        "CASE WHEN pk.column_name IS NULL THEN '' ELSE 'PRI' END AS col_key "
                        "FROM information_schema.columns c "
                        "LEFT JOIN ("
                        "  SELECT kcu.column_name "
                        "  FROM information_schema.table_constraints tc "
                        "  JOIN information_schema.key_column_usage kcu "
                        "    ON tc.constraint_name = kcu.constraint_name "
                        "   AND tc.table_schema = kcu.table_schema "
                        "   AND tc.table_name = kcu.table_name "
                        "  WHERE tc.constraint_type = 'PRIMARY KEY' "
                        "    AND tc.table_schema = current_schema() "
                        "    AND tc.table_name = %s"
                        ") pk ON pk.column_name = c.column_name "
                        "WHERE c.table_schema = current_schema() AND c.table_name = %s "
                        "ORDER BY c.ordinal_position",
                        (tname, tname),
                    )
                    columns = [
                        ColumnInfo(
                            name=col[0],
                            type=str(col[1]),
                            nullable=(col[2] == "YES"),
                            primary_key=(col[3] == "PRI"),
                        )
                        for col in await cur.fetchall()
                    ]
                    tables.append(TableInfo(
                        name=tname,
                        schema=str(self.config.get("database", "")),
                        columns=columns,
                        # reltuples 在「从未 VACUUM/ANALYZE 过」时是 -1(官方
                        # 文档的哨兵),不是 0 —— 原样透出会让 catalog 报 -1 行。
                        row_count_estimate=positive_int(row_count),
                    ))
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"PostgreSQL schema introspection failed: {e}",
                datasource=self.name,
            ) from e

        return SchemaInfo(tables=tables)

    async def table_profiles(self) -> dict[str, TableProfile]:
        """画像:行数 + 表字节数 + 统计收集时间(§8.2 B 的方言实现)。

        **不与 ``get_schema`` 合并**:``get_schema`` 的列信息是每表一个往返,
        而画像要的三个量一次目录扫描就够。合并看着省事,代价是每次画像刷新
        (TTL 5min)都付一遍架构内省的钱(§10)。

        ``pg_relation_size`` 而不是 ``pg_total_relation_size``:后者含索引与
        TOAST,而这里要的是「顺序读这张表要碰多少字节」的量纲。用总量会高估一次
        不带索引的扫描。

        分区表(``relkind = 'p'``)本身没有存储,行数与字节都会是「没有依据」;
        真正的量在各分区上 —— 分区是 ``relkind = 'r'``,和普通表一起出现在
        结果里。
        """
        await self._ensure_connected()
        caps = self.profile_capabilities
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    "SELECT c.relname, c.reltuples::bigint AS row_count, "
                    "pg_relation_size(c.oid) AS bytes, "
                    "s.last_analyze "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
                    "WHERE n.nspname = current_schema() AND c.relkind = 'r' "
                    "ORDER BY c.relname"
                )
                rows = await cur.fetchall()
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"PostgreSQL profile introspection failed: {e}",
                datasource=self.name,
            ) from e

        return {
            str(name): TableProfile(
                table=str(name),
                row_count=positive_int(row_count),
                bytes=positive_int(size),
                last_analyzed=timestamp_str(last_analyzed),
                capabilities=caps,
            )
            for name, row_count, size, last_analyzed in rows
        }

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(
            supports_cte=True,
            supports_window_functions=True,
            supports_transactions=True,
            supports_json_type=True,
            dialect="postgres",
        )
