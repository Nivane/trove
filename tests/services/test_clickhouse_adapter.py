"""ClickHouse adapter tests — unit (fake driver) + integration (CLICKHOUSE_TEST_URL)."""

import os
import uuid

import pytest

from trove.core.errors import DatasourceError, SQLExecutionError
from trove.services.datasource.adapters import clickhouse as ch_module
from trove.services.datasource.adapters.clickhouse import ClickHouseAdapter


class FakeResult:
    def __init__(self, column_names, result_rows):
        self.column_names = column_names
        self.result_rows = result_rows


class FakeClient:
    def __init__(self, scripted=None):
        # scripted: list of FakeResult returned per query() call
        self._scripted = list(scripted or [])
        self.queries = []

    def query(self, sql, parameters=None):
        self.queries.append((sql, parameters))
        return self._scripted.pop(0) if self._scripted else FakeResult([], [])

    def close(self):
        self.closed = True


class FakeDriver:
    def __init__(self, client=None):
        self.client = client or FakeClient()
        self.get_client_kwargs = None

    def get_client(self, **kwargs):
        self.get_client_kwargs = kwargs
        return self.client


def make_adapter(monkeypatch, driver=None, config=None):
    driver = driver or FakeDriver()
    monkeypatch.setattr(ch_module, "_get_driver", lambda: driver)
    adapter = ClickHouseAdapter(
        name="test",
        config=config or {
            "host": "127.0.0.1", "port": 8123,
            "user": "default", "password": "p", "database": "testdb",
        },
    )
    return adapter, driver


class TestClickHouseAdapter:
    async def test_connect_passes_parsed_params(self, monkeypatch):
        adapter, driver = make_adapter(monkeypatch)
        await adapter.connect()

        assert adapter.is_connected
        kwargs = driver.get_client_kwargs
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 8123
        assert kwargs["username"] == "default"
        assert kwargs["password"] == "p"
        assert kwargs["database"] == "testdb"

    async def test_connect_failure_wraps_datasource_error(self, monkeypatch):
        class BadDriver:
            def get_client(self, **kwargs):
                raise OSError("connection refused")

        monkeypatch.setattr(ch_module, "_get_driver", lambda: BadDriver())
        adapter = ClickHouseAdapter(name="test", config={})
        with pytest.raises(DatasourceError):
            await adapter.connect()
        assert not adapter.is_connected

    async def test_execute_returns_query_result(self, monkeypatch):
        client = FakeClient([FakeResult(["id", "name"], [[1, "a"], [2, "b"]])])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()

        result = await adapter.execute("SELECT id, name FROM t")
        assert result.columns == ["id", "name"]
        assert result.rows == [[1, "a"], [2, "b"]]
        assert result.row_count == 2
        assert client.queries[0][0] == "SELECT id, name FROM t"

    async def test_execute_error_wraps(self, monkeypatch):
        class ExplodingClient:
            def query(self, sql, parameters=None):
                raise RuntimeError("table not found")

        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(ExplodingClient()))
        await adapter.connect()

        with pytest.raises(SQLExecutionError) as exc_info:
            await adapter.execute("SELECT * FROM ghost")
        assert "table not found" in str(exc_info.value)

    async def test_execute_before_connect_raises(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        with pytest.raises(SQLExecutionError):
            await adapter.execute("SELECT 1")

    async def test_get_schema_introspects_system_tables(self, monkeypatch):
        client = FakeClient([
            FakeResult(["name", "total_rows"], [("events", 1000)]),
            FakeResult(
                ["name", "type", "is_in_primary_key"],
                [("id", "UInt64", 1), ("name", "String", 0)],
            ),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()

        schema = await adapter.get_schema()
        assert len(schema.tables) == 1
        table = schema.tables[0]
        assert table.name == "events"
        assert table.row_count_estimate == 1000

        id_col = next(c for c in table.columns if c.name == "id")
        assert id_col.primary_key is True

        name_col = next(c for c in table.columns if c.name == "name")
        assert name_col.primary_key is False

        executed = " ".join(sql for sql, _ in client.queries)
        assert "system.tables" in executed
        assert "system.columns" in executed

    async def test_get_capabilities(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        caps = await adapter.get_capabilities()
        assert caps.dialect == "clickhouse"
        assert caps.supports_cte is True
        assert caps.supports_window_functions is True
        assert caps.supports_transactions is False
        assert caps.supports_json_type is True

    async def test_dialect_static(self):
        assert ClickHouseAdapter.dialect() == "clickhouse"

    async def test_missing_driver_hint(self, monkeypatch):
        def _missing():
            raise DatasourceError(
                message="clickhouse-connect is not installed — run `uv sync --extra clickhouse`",
                datasource="",
            )

        monkeypatch.setattr(ch_module, "_get_driver", _missing)
        adapter = ClickHouseAdapter(name="test", config={})
        with pytest.raises(DatasourceError) as exc_info:
            await adapter.connect()
        assert "uv sync --extra clickhouse" in str(exc_info.value)

    async def test_disconnect_closes_client(self, monkeypatch):
        client = FakeClient()
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()
        await adapter.disconnect()
        assert not adapter.is_connected
        assert client.closed is True


# ── Integration tests (CLICKHOUSE_TEST_URL, skipped when unset) ──


@pytest.mark.integration
class TestClickHouseIntegration:
    @pytest.fixture
    async def clickhouse_env(self):
        url = os.environ.get("CLICKHOUSE_TEST_URL")
        if not url:
            pytest.skip("CLICKHOUSE_TEST_URL not set")
        from trove.services.datasource.urls import parse_datasource_url
        return parse_datasource_url(url)

    async def test_full_lifecycle_against_real_clickhouse(self, clickhouse_env):
        import clickhouse_connect

        params = clickhouse_env.connection_params
        test_db = f"trove_test_{uuid.uuid4().hex[:8]}"

        admin = clickhouse_connect.get_client(
            host=params["host"], port=params["port"],
            username=params["user"], password=params["password"],
        )
        try:
            admin.command(f"CREATE DATABASE {test_db}")

            adapter = ClickHouseAdapter(
                name=test_db, config={**params, "database": test_db},
            )
            await adapter.connect()
            try:
                await adapter.execute(
                    "CREATE TABLE students (id UInt64, name String) "
                    "ENGINE = MergeTree ORDER BY id"
                )
                await adapter.execute(
                    "INSERT INTO students VALUES (1, 'Alice'), (2, 'Bob')"
                )

                result = await adapter.execute("SELECT * FROM students ORDER BY id")
                assert result.row_count == 2
                assert result.columns == ["id", "name"]

                schema = await adapter.get_schema()
                table = next(t for t in schema.tables if t.name == "students")
                assert len(table.columns) == 2
            finally:
                await adapter.disconnect()
        finally:
            admin.command(f"DROP DATABASE IF EXISTS {test_db}")
            admin.close()
