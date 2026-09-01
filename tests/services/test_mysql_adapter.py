"""MySQL adapter tests — unit (fake driver) + integration (MYSQL_TEST_URL)."""

import os
import uuid

import pytest

from trove.core.errors import DatasourceError, SQLExecutionError
from trove.services.datasource.adapters.mysql import MySQLAdapter


# ── Fake driver ──────────────────────────────────────────


class FakeCursor:
    def __init__(self, responses=None, description=None, error=None):
        self._responses = list(responses or [])
        self.description = description
        self.error = error
        self.executed = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.error:
            raise self.error

    async def fetchall(self):
        return self._responses.pop(0) if self._responses else []

    async def fetchone(self):
        return self._responses.pop(0) if self._responses else None

    async def close(self):
        pass


class FakeConn:
    def __init__(self, cursor_specs=None):
        # cursor_specs: list of (responses, description, error) per cursor creation
        self._cursor_specs = list(cursor_specs or [])
        self.cursors = []
        self.ping_count = 0
        self.ping_error = None

    async def ping(self, reconnect=True):
        self.ping_count += 1
        if self.ping_error:
            raise self.ping_error

    async def cursor(self):
        spec = self._cursor_specs.pop(0) if self._cursor_specs else ({}, None, None)
        responses, description, error = spec
        cur = FakeCursor(responses, description, error)
        self.cursors.append(cur)
        return cur

    async def close(self):
        pass


class FakeDriver:
    def __init__(self, conn=None):
        self.conn = conn or FakeConn()
        self.connect_kwargs = None

    async def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        return self.conn


def make_adapter(monkeypatch, driver=None, config=None, version="8.0.36"):
    driver = driver or FakeDriver()
    monkeypatch.setattr(MySQLAdapter, "_get_driver", staticmethod(lambda: driver))
    adapter = MySQLAdapter(
        name="test",
        config=config or {
            "host": "127.0.0.1", "port": 3306,
            "user": "root", "password": "p", "database": "testdb",
        },
    )
    adapter._server_version = version
    return adapter, driver


# ── Unit tests ───────────────────────────────────────────


class TestMySQLAdapter:
    async def test_connect_passes_parsed_params(self, monkeypatch):
        adapter, driver = make_adapter(monkeypatch)
        await adapter.connect()

        assert adapter.is_connected
        kwargs = driver.connect_kwargs
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 3306
        assert kwargs["user"] == "root"
        assert kwargs["password"] == "p"
        assert kwargs["db"] == "testdb"

    async def test_connect_probes_version(self, monkeypatch):
        conn = FakeConn(cursor_specs=[([["8.0.36"]], None, None)])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        adapter._server_version = ""
        await adapter.connect()

        assert adapter._server_version == "8.0.36"
        assert conn.cursors[0].executed[0][0] == "SELECT VERSION()"

    async def test_connect_failure_wraps_datasource_error(self, monkeypatch):
        class BadDriver:
            async def connect(self, **kwargs):
                raise OSError("connection refused")

        monkeypatch.setattr(MySQLAdapter, "_get_driver", staticmethod(lambda: BadDriver()))
        adapter = MySQLAdapter(name="test", config={})
        with pytest.raises(DatasourceError):
            await adapter.connect()
        assert not adapter.is_connected

    async def test_execute_returns_query_result(self, monkeypatch):
        conn = FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),  # version probe
            ([[[1, "a"], [2, "b"]]], [("id",), ("name",)], None),  # execute
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
            ([["8.0.36"]], None, None),
            ([], None, RuntimeError("no such table")),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        with pytest.raises(SQLExecutionError) as exc_info:
            await adapter.execute("SELECT * FROM ghost")
        assert "no such table" in str(exc_info.value)

    async def test_execute_before_connect_raises(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        with pytest.raises(SQLExecutionError):
            await adapter.execute("SELECT 1")

    async def test_execute_reconnects_stale_connection(self, monkeypatch):
        conn = FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),  # version probe
            ([[[1, "a"]]], [("id",)], None),  # execute after reconnect
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()
        adapter._conn.close()  # simulate server dropping the idle connection

        result = await adapter.execute("SELECT id FROM t")
        assert result.rows == [[1, "a"]]
        assert conn.ping_count >= 1

    async def test_reconnect_failure_raises_datasource_error(self, monkeypatch):
        conn = FakeConn()
        conn.ping_error = RuntimeError("connection refused")
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()
        adapter._conn.close()

        with pytest.raises(DatasourceError, match="reconnect failed"):
            await adapter.execute("SELECT 1")

    async def test_get_schema_reconnects_stale_connection(self, monkeypatch):
        conn = FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),                       # version probe cursor
            (                                               # get_schema cursor (reused):
                [
                    [("students", 100)],
                    [("id", "int", "NO", "PRI")],
                ],
                None, None,
            ),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()
        adapter._conn.close()

        schema = await adapter.get_schema()
        assert len(schema.tables) == 1
        assert schema.tables[0].name == "students"
        assert conn.ping_count >= 1

    async def test_get_schema_introspects_information_schema(self, monkeypatch):
        conn = FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),                       # version probe cursor
            (                                               # get_schema cursor (reused):
                [
                    [("students", 100)],                     #   TABLES fetchall
                    [                                       #   COLUMNS fetchall
                        ("id", "int", "NO", "PRI"),
                        ("name", "varchar", "YES", ""),
                    ],
                ],
                None, None,
            ),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        schema = await adapter.get_schema()
        assert len(schema.tables) == 1
        table = schema.tables[0]
        assert table.name == "students"
        assert table.row_count_estimate == 100

        # introspection went through information_schema
        executed_sql = " ".join(
            sql for cur in conn.cursors for sql, _ in cur.executed
        )
        assert "information_schema.TABLES" in executed_sql
        assert "information_schema.COLUMNS" in executed_sql

        id_col = next(c for c in table.columns if c.name == "id")
        assert id_col.primary_key is True
        assert id_col.nullable is False

        name_col = next(c for c in table.columns if c.name == "name")
        assert name_col.nullable is True
        assert name_col.primary_key is False

    async def test_get_capabilities_mysql8(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch, version="8.0.36")
        caps = await adapter.get_capabilities()
        assert caps.dialect == "mysql"
        assert caps.supports_cte is True
        assert caps.supports_window_functions is True
        assert caps.supports_json_type is True

    async def test_get_capabilities_mysql57(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch, version="5.7.44")
        caps = await adapter.get_capabilities()
        assert caps.supports_cte is False
        assert caps.supports_window_functions is False

    async def test_dialect_static(self):
        assert MySQLAdapter.dialect() == "mysql"

    async def test_missing_driver_hint(self, monkeypatch):
        def _missing():
            raise DatasourceError(
                message="aiomysql is not installed — run `uv sync --extra mysql`",
                datasource="",
            )

        monkeypatch.setattr(MySQLAdapter, "_get_driver", staticmethod(_missing))
        adapter = MySQLAdapter(name="test", config={})
        with pytest.raises(DatasourceError) as exc_info:
            await adapter.connect()
        assert "uv sync --extra mysql" in str(exc_info.value)


# ── Integration tests (MYSQL_TEST_URL, skipped when unset) ──


@pytest.mark.integration
class TestMySQLIntegration:
    @pytest.fixture
    async def mysql_env(self):
        url = os.environ.get("MYSQL_TEST_URL")
        if not url:
            pytest.skip("MYSQL_TEST_URL not set")
        from trove.services.datasource.urls import parse_datasource_url
        cfg = parse_datasource_url(url)
        return cfg

    async def test_full_lifecycle_against_real_mysql(self, mysql_env):
        import aiomysql

        params = mysql_env.connection_params
        test_db = f"trove_test_{uuid.uuid4().hex[:8]}"

        # Isolated test database (requires CREATE DATABASE rights)
        admin = await aiomysql.connect(
            host=params["host"], port=params["port"],
            user=params["user"], password=params["password"],
        )
        try:
            cur = await admin.cursor()
            await cur.execute(f"CREATE DATABASE `{test_db}`")
            await cur.close()

            adapter = MySQLAdapter(name=test_db, config={**params, "database": test_db})
            await adapter.connect()
            try:
                await adapter.execute(
                    "CREATE TABLE students (id INT PRIMARY KEY, name VARCHAR(64) NOT NULL)"
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
            cur = await admin.cursor()
            await cur.execute(f"DROP DATABASE IF EXISTS `{test_db}`")
            await cur.close()
            admin.close()
