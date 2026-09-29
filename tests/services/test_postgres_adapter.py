"""PostgreSQL adapter tests — unit (fake driver) + integration (PG_TEST_URL)."""

import os
import re
import uuid
from types import SimpleNamespace

import pytest

from trove.core.errors import DatasourceError, SQLExecutionError
from trove.core.types import BASIS_GRANTS, BASIS_PROBE_FAILED
from trove.services.datasource.adapters import postgres as pg_module
from trove.services.datasource.adapters.postgres import (
    PostgresAdapter,
    _conninfo,
)


# ── Fake driver ──────────────────────────────────────────


class FakeCursor:
    def __init__(self, responses=None, description=None, error=None):
        self._responses = list(responses or [])
        self.description = description
        self.error = error
        self.executed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.error:
            raise self.error

    async def fetchall(self):
        return self._responses.pop(0) if self._responses else []


class FakeConn:
    def __init__(self, cursor_specs=None):
        # cursor_specs: list of (responses, description, error) per cursor creation
        self._cursor_specs = list(cursor_specs or [])
        self.cursors = []
        self.closed = False

    def cursor(self):  # psycopg AsyncConnection.cursor() is sync → AsyncCursor
        spec = self._cursor_specs.pop(0) if self._cursor_specs else ({}, None, None)
        cur = FakeCursor(*spec)
        self.cursors.append(cur)
        return cur

    async def close(self):
        self.closed = True


class FakeDriver:
    def __init__(self, conn=None):
        self.conn = conn or FakeConn()
        self.connect_args = None
        self.connect_count = 0
        driver = self

        class _AsyncConnection:
            @classmethod
            async def connect(cls, conninfo):
                driver.connect_args = conninfo
                driver.connect_count += 1
                return driver.conn

        self.AsyncConnection = _AsyncConnection


def make_adapter(monkeypatch, driver=None, config=None):
    driver = driver or FakeDriver()
    monkeypatch.setattr(pg_module, "_get_driver", lambda: driver)
    adapter = PostgresAdapter(
        name="test",
        config=config or {
            "host": "127.0.0.1", "port": 5432,
            "user": "trove", "password": "p", "database": "testdb",
        },
    )
    return adapter, driver


# ── Unit tests ───────────────────────────────────────────


class TestPostgresAdapter:
    async def test_conninfo_builds_dsn(self):
        info = _conninfo({
            "host": "pg", "port": 5432,
            "user": "trove", "password": "p", "database": "testdb",
        })
        assert info == "postgresql://trove:p@pg:5432/testdb"

    async def test_connect_passes_conninfo(self, monkeypatch):
        adapter, driver = make_adapter(monkeypatch)
        await adapter.connect()
        assert adapter.is_connected
        assert "postgresql://trove:p@127.0.0.1:5432/testdb" in driver.connect_args

    async def test_connect_failure_wraps_datasource_error(self, monkeypatch):
        class BadDriver:
            class AsyncConnection:
                @classmethod
                async def connect(cls, conninfo):
                    raise OSError("connection refused")

        monkeypatch.setattr(pg_module, "_get_driver", lambda: BadDriver())
        adapter = PostgresAdapter(name="test", config={})
        with pytest.raises(DatasourceError):
            await adapter.connect()
        assert not adapter.is_connected

    async def test_execute_returns_query_result(self, monkeypatch):
        conn = FakeConn(cursor_specs=[
            ([[[1, "a"], [2, "b"]]], [SimpleNamespace(name="id"), SimpleNamespace(name="name")], None),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        result = await adapter.execute("SELECT id, name FROM t")
        assert result.columns == ["id", "name"]
        assert result.rows == [[1, "a"], [2, "b"]]
        assert result.row_count == 2
        assert result.datasource == "test"

    async def test_execute_error_wraps(self, monkeypatch):
        conn = FakeConn(cursor_specs=[
            ([], None, RuntimeError("relation ghost does not exist")),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        with pytest.raises(SQLExecutionError) as exc_info:
            await adapter.execute("SELECT * FROM ghost")
        assert "ghost" in str(exc_info.value)

    async def test_execute_before_connect_raises(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        with pytest.raises(SQLExecutionError):
            await adapter.execute("SELECT 1")

    async def test_execute_reconnects_stale_connection(self, monkeypatch):
        conn = FakeConn(cursor_specs=[
            ([[[1, "a"]]], [SimpleNamespace(name="id")], None),
        ])
        adapter, driver = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()
        conn.closed = True  # simulate server dropping the idle connection

        result = await adapter.execute("SELECT id FROM t")
        assert result.rows == [[1, "a"]]
        assert driver.connect_count >= 2

    async def test_get_schema_introspects_information_schema(self, monkeypatch):
        # get_schema 复用同一个 cursor:fetchall 按序返回 表清单 → 该表的列
        conn = FakeConn(cursor_specs=[
            ([
                [("students", 100)],                       # 1st fetchall: tables
                [                                           # 2nd fetchall: columns
                    ("id", "integer", "NO", "PRI"),
                    ("name", "text", "YES", ""),
                ],
            ], None, None),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        schema = await adapter.get_schema()
        assert len(schema.tables) == 1
        table = schema.tables[0]
        assert table.name == "students"
        assert table.row_count_estimate == 100

        executed_sql = " ".join(
            sql for cur in conn.cursors for sql, _ in cur.executed
        )
        assert "pg_class" in executed_sql
        assert "information_schema.columns" in executed_sql

        id_col = next(c for c in table.columns if c.name == "id")
        assert id_col.primary_key is True
        assert id_col.nullable is False

        name_col = next(c for c in table.columns if c.name == "name")
        assert name_col.nullable is True
        assert name_col.primary_key is False

    async def test_get_capabilities(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        caps = await adapter.get_capabilities()
        assert caps.dialect == "postgres"
        assert caps.supports_cte is True
        assert caps.supports_window_functions is True
        assert caps.supports_transactions is True
        assert caps.supports_json_type is True

    async def test_dialect_static(self):
        assert PostgresAdapter.dialect() == "postgres"

    async def test_missing_driver_hint(self, monkeypatch):
        def _missing():
            raise DatasourceError(
                message="psycopg is not installed — run `uv sync --extra postgres`",
                datasource="",
            )

        monkeypatch.setattr(pg_module, "_get_driver", _missing)
        adapter = PostgresAdapter(name="test", config={})
        with pytest.raises(DatasourceError) as exc_info:
            await adapter.connect()
        assert "uv sync --extra postgres" in str(exc_info.value)


# ── Readonly probe (设计 §4 I1)───────────────────────────


class TestPostgresReadonlyProbe:
    """只读自检的 PG 实现(设计 §4 I1)。

    与 MySQL 那份的**分工不同**:MySQL 拿到的是一行行「授权原文」,PG 拿到的
    是**三个计数**(可达的超管角色 / 属主关系 / 写权限条目)。计数没法像授权
    原文那样整条留在 detail 里,所以这一档的每条用例都断言 detail 里出现的
    **具体数字** —— 形状一错(假驱动少包一层,「三列一行」变成「三个整数」),
    数字就对不上,用例不会因为「遍历到的东西里没有关键字」而假绿。MySQL 那边
    正是这么假绿过的(见 ``TestMySQLReadonlyProbe._probe_conn``)。
    """

    @staticmethod
    def _probe_conn(row):
        """一个已连上、且下一次 cursor 会吐出这一行的连接。

        ``[[row]]``:内层 ``row`` 是**三列一行**,外层才是 ``FakeCursor`` 记的
        一次 ``fetchall`` 的返回值(= 行的列表)。PG 的 ``connect`` 不发查询
        (没有版本探测),所以探测用的就是**第一个** cursor。
        """
        return FakeConn(cursor_specs=[([[row]], None, None)])

    @staticmethod
    def _failing_conn(error):
        """两次取 cursor 都失败:第一次直接探(该抛),第二次经编排器(该折)。"""
        return FakeConn(cursor_specs=[([], None, error)] * 2)

    def _adapter(self, monkeypatch, conn):
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        return adapter

    async def test_a_read_only_account_is_verified(self, monkeypatch):
        conn = self._probe_conn([0, 0, 0])
        adapter = self._adapter(monkeypatch, conn)
        await adapter.connect()

        probe = await adapter.probe_readonly()

        assert probe.verified is True, "只读账号必须被判成只读"
        assert probe.basis == BASIS_GRANTS, "只有查了权限表才配叫 verified"

    async def test_an_account_that_can_write_is_reported_as_writable(self, monkeypatch):
        conn = self._probe_conn([0, 0, 2])
        adapter = self._adapter(monkeypatch, conn)
        await adapter.connect()

        probe = await adapter.probe_readonly()

        assert probe.verified is False, "写得动的账号不能被说成只读"
        # 依据要留在 detail 里:数字对不上就说明三列被读串了
        assert "2 write privilege" in probe.detail

    async def test_a_superuser_is_not_a_read_only_boundary(self, monkeypatch):
        """超管是**独立于 GRANT** 的一条写路径:一条授权都没授,照样能写。"""
        conn = self._probe_conn([1, 0, 0])
        adapter = self._adapter(monkeypatch, conn)
        await adapter.connect()

        probe = await adapter.probe_readonly()

        assert probe.verified is False
        assert "superuser" in probe.detail

    async def test_owning_one_relation_is_enough_to_write(self, monkeypatch):
        """**这条最容易漏**:属主天然可写,跟 GRANT 一点关系都没有。

        一个「0 写权限」的账号完全可能是某张表的属主 —— 只看权限表就会把它
        报成只读。所以属主是独立的一项。
        """
        conn = self._probe_conn([0, 1, 0])
        adapter = self._adapter(monkeypatch, conn)
        await adapter.connect()

        probe = await adapter.probe_readonly()

        assert probe.verified is False
        assert "owns 1 relation" in probe.detail

    async def test_the_probe_sends_exactly_one_read_only_statement(self, monkeypatch):
        """**探测绝不试写**(§4 I1 的实施取舍)。

        设计稿给的另一条路是「尝试一条必然失败的写语句」—— 它「必然失败」的
        前提正是「账号只读」这个待证假设。账号其实可写时,那条语句会**真的写
        进去**:一个探测变成一次生产写入。

        钉法比 MySQL 那边要绕一点:PG 的这条查询里**必须**出现
        ``'INSERT'``/``'UPDATE'`` 这些词(它们是 ``privilege_type`` 的取值),
        所以不能直接扫关键字。改钉两条更硬的性质:整个探测**只有一条语句、
        且以 ``SELECT`` 开头**;把字符串字面量摘掉之后,写语句的关键词一个都
        不剩。

        「摘字面量」之后再按**词边界**扫:摘完还剩 ``grantee`` 这个列名,
        按子串扫会把 ``grantee`` 里的 ``grant`` 认成 GRANT 语句 —— 这条断言
        写第一版时就是这么假红的。
        """
        conn = self._probe_conn([0, 0, 0])
        adapter = self._adapter(monkeypatch, conn)
        await adapter.connect()
        await adapter.probe_readonly()

        assert len(conn.cursors) == 1, "探测只该取一个 cursor"
        sent = [sql for sql, _ in conn.cursors[0].executed]
        assert len(sent) == 1, sent
        assert sent[0].lstrip().upper().startswith("SELECT"), sent[0]

        without_literals = re.sub(r"'[^']*'", "''", sent[0]).upper()
        found = re.findall(
            r"\b(?:INSERT|UPDATE|DELETE|TRUNCATE|CREATE|DROP|ALTER|GRANT|REVOKE)\b",
            without_literals,
        )
        assert found == [], (found, sent[0])

    async def test_the_query_covers_public_and_inherited_and_column_grants(
        self, monkeypatch,
    ):
        """查询本身要覆盖三条**除「直接授予当前角色」之外**的写路径。

        这是唯一能钉住它们的用例:假驱动给什么数就是什么数,把
        ``grantee = current_user``(那正是 §4 I1 草稿的写法)改回来,上面所有
        用例照样全绿 —— 而线上会把一个**通过组角色拿到 INSERT** 的账号报成
        只读。所以这里断言 SQL 文本:

        * ``'PUBLIC'`` —— ``GRANT ... TO PUBLIC`` 的行 ``grantee`` 是字面量
          ``'PUBLIC'``,不等于 ``current_user``;
        * ``pg_has_role`` —— 授予**我所属的角色**的权限,行上写的是那个角色名;
        * ``column_privileges`` —— 列级授权不在 ``table_privileges`` 里。
        """
        conn = self._probe_conn([0, 0, 0])
        adapter = self._adapter(monkeypatch, conn)
        await adapter.connect()
        await adapter.probe_readonly()

        sql = conn.cursors[0].executed[0][0]
        assert "grantee = 'PUBLIC'" in sql
        assert "pg_has_role" in sql
        assert "column_privileges" in sql
        assert "table_privileges" in sql
        # 三项各自独立的来源,少一项就漏判一类
        for source in ("rolsuper", "relowner", "privilege_type"):
            assert source in sql, source
        assert "grantee = current_user" not in sql, "那可漏掉继承与 PUBLIC"

    async def test_the_privilege_test_is_an_exact_match_not_a_keyword_scan(
        self, monkeypatch,
    ):
        """权限判据必须是 ``privilege_type`` 的**精确取值匹配**,不是扫关键字。

        这是 MySQL 那条陷阱在 PG 上的对应物:``GRANT SELECT ON public.insert_log``
        里的 ``insert_log`` 是**表名**。整行(或表名拼权限)扫关键字的实现会把
        一个纯只读账号报成可写 —— 方向上是保守的(误报),但同样是错,而且会
        让真出现的那条 WARN 失去意义。

        为什么在这里是**文本断言**而不是行为断言:PG 的过滤在 SQL 里做(一次
        往返、只回三列),假驱动给什么数就是什么数,看不到这句 SQL 干了什么 ——
        所以只能钉 SQL 文本。这条与 ``test_the_query_covers_...`` 一起,把
        「查询被悄悄改窄/改歪」这一类改动挡在测试这一层。
        """
        conn = self._probe_conn([0, 0, 0])
        adapter = self._adapter(monkeypatch, conn)
        await adapter.connect()
        await adapter.probe_readonly()

        sql = conn.cursors[0].executed[0][0]
        assert "privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')" in sql
        assert "privilege_type IN ('INSERT', 'UPDATE')" in sql
        assert "LIKE" not in sql.upper(), "扫关键字就会把表名当成权限"

    async def test_an_empty_result_is_not_read_only(self, monkeypatch):
        """一行都没拿到 → **取不到**,不是「确认只读」。

        最可能的解释是这条路径没拿到东西(驱动/方言/被包装),而不是「这个账号
        什么都不能干」。往 ``True`` 倒就是替一道并不存在的边界背书。
        """
        conn = FakeConn(cursor_specs=[([], None, None)])
        adapter = self._adapter(monkeypatch, conn)
        await adapter.connect()

        with pytest.raises(DatasourceError):
            await adapter.probe_readonly()

    async def test_a_short_row_raises(self, monkeypatch):
        """列数不对(这里少一列)→ 抛,不猜缺的是哪一列。"""
        conn = self._probe_conn([0, 0])
        adapter = self._adapter(monkeypatch, conn)
        await adapter.connect()

        with pytest.raises(DatasourceError):
            await adapter.probe_readonly()

    async def test_more_than_one_row_raises(self, monkeypatch):
        """多行 → 抛。这条查询**结构上**只该回一行,回两行说明问错了东西。"""
        conn = FakeConn(cursor_specs=[([[0, 0, 0], [1, 0, 0]], None, None)])
        adapter = self._adapter(monkeypatch, conn)
        await adapter.connect()

        with pytest.raises(DatasourceError):
            await adapter.probe_readonly()

    async def test_non_count_values_raise(self, monkeypatch):
        """三列必须是计数。``True``/``"0"``/``None`` 都不是 —— 一个读不出来的
        值就是「取不到」,不能当成 0 用。"""
        for bad in ([True, 0, 0], [0, "0", 0], [0, 0, None], [0, -1, 0]):
            conn = self._probe_conn(bad)
            adapter = self._adapter(monkeypatch, conn)
            await adapter.connect()
            with pytest.raises(DatasourceError):
                await adapter.probe_readonly()

    async def test_a_failing_query_is_folded_by_the_orchestrator(self, monkeypatch):
        """查询被拒(权限不足)/ 连接断 → 适配器**如实抛**,由 ``readonly.probe``
        折成 ``probe_failed``。

        在这里吞掉它,就等于让「没查成」消失在实现里 —— 而它折出来的是一个
        看得见、会 WARN 的状态。
        """
        conn = self._failing_conn(RuntimeError("permission denied for table pg_class"))
        adapter = self._adapter(monkeypatch, conn)
        await adapter.connect()

        with pytest.raises(RuntimeError):
            await adapter.probe_readonly()

        from trove.services.datasource import readonly

        probe = await readonly.probe(adapter)
        assert probe.verified is None
        assert probe.basis == BASIS_PROBE_FAILED


# ── Integration tests (PG_TEST_URL, skipped when unset) ──


@pytest.mark.integration
class TestPostgresIntegration:
    @pytest.fixture
    async def pg_env(self):
        url = os.environ.get("PG_TEST_URL")
        if not url:
            pytest.skip("PG_TEST_URL not set")
        from trove.services.datasource.urls import parse_datasource_url
        return parse_datasource_url(url)

    async def test_full_lifecycle_against_real_postgres(self, pg_env):
        import psycopg

        params = pg_env.connection_params
        test_db = f"trove_test_{uuid.uuid4().hex[:8]}"

        admin = await psycopg.AsyncConnection.connect(
            _conninfo({**params, "database": "postgres"})
        )
        try:
            async with admin.cursor() as cur:
                await cur.execute(f'CREATE DATABASE "{test_db}"')

            adapter = PostgresAdapter(name=test_db, config={**params, "database": test_db})
            await adapter.connect()
            try:
                await adapter.execute(
                    "CREATE TABLE students (id INT PRIMARY KEY, name TEXT NOT NULL)"
                )
                await adapter.execute(
                    "INSERT INTO students VALUES (1, 'Alice'), (2, 'Bob')"
                )

                result = await adapter.execute("SELECT * FROM students ORDER BY id")
                assert result.row_count == 2
                assert result.columns == ["id", "name"]

                schema = await adapter.get_schema()
                table = next(t for t in schema.tables if t.name == "students")
                id_col = next(c for c in table.columns if c.name == "id")
                assert id_col.primary_key is True
            finally:
                await adapter.disconnect()
        finally:
            async with admin.cursor() as cur:
                await cur.execute(f'DROP DATABASE IF EXISTS "{test_db}"')
            await admin.close()


class TestPostgresProfile:
    """PostgreSQL 的画像字段(执行画像 §8.2 B / §14.12)。

    这一档要的两个字段都有**官方文档背书的哨兵值**,不是猜的:

    * ``pg_class.reltuples``:「If the table has never yet been vacuumed or
      analyzed, reltuples contains -1 indicating that the row count is unknown.」
      存量写 ``int(row_count or 0)`` —— -1 是真值,于是**对外报 -1 行**:
      ``catalog`` 原样透出、检索文档写成 ``(approx -1 rows)``、``refuse`` 的
      自动执行阈值拿 ``-1 > 上限`` 判成「小表」。方向恰好是放宽。
    * ``pg_stat_user_tables.last_analyze``:没收集过统计就是 NULL。它同时是
      「上面那个行数为什么不作数」的**说明** —— 所以是独立字段,不是装饰。
    """

    async def test_missing_stats_are_none_not_negative(self, monkeypatch):
        conn = FakeConn(cursor_specs=[([
            [("never_analyzed", -1)],
            [("id", "integer", "NO", "PRI")],
        ], None, None)])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        schema = await adapter.get_schema()
        assert schema.tables[0].row_count_estimate is None

    async def test_profile_reports_bytes_and_last_analyzed(self, monkeypatch):
        from datetime import datetime

        conn = FakeConn(cursor_specs=[([
            [("events", 5_000_000, 12 * 1024**3, datetime(2026, 9, 20, 8, 0))],
        ], None, None)])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        p = (await adapter.table_profiles())["events"]
        assert p.row_count == 5_000_000
        assert p.bytes == 12 * 1024**3
        assert p.last_analyzed == "2026-09-20T08:00:00"
        # PG 的目录里**没有**「数据最后修改时间」这个字段:不声明,也不编一个。
        assert p.last_modified is None

        sql = " ".join(s for c in conn.cursors for s, _ in c.executed)
        assert "pg_relation_size" in sql
        assert "last_analyze" in sql

    async def test_bytes_survive_where_row_counts_do_not(self, monkeypatch):
        """没 analyze 的表:行数与统计时间都没有,但**字节数还在**。

        这是两条来源的差别,不是遗漏:``pg_relation_size`` 读的是文件系统的
        事实,不依赖统计收集。成本轨因此在统计缺失时仍有一个真实的量纲可用,
        不必直接掉到保守预算 —— 降级链的第 2 档本来就是为这种时刻准备的。
        """
        conn = FakeConn(cursor_specs=[([
            [("ghost", -1, 8 * 1024 * 1024, None)],
        ], None, None)])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        p = (await adapter.table_profiles())["ghost"]
        assert p.row_count is None
        assert p.bytes == 8 * 1024 * 1024
        assert p.last_analyzed is None
