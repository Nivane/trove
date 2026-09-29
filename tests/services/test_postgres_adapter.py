"""PostgreSQL adapter tests — unit (fake driver) + integration (PG_TEST_URL)."""

import os
import uuid
from types import SimpleNamespace

import pytest

from trove.core.errors import DatasourceError, SQLExecutionError
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
