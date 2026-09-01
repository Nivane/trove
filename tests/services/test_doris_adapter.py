"""Doris adapter tests — unit (fake driver, MySQL wire protocol).

Doris speaks the MySQL protocol, so the unit surface mirrors the MySQL
adapter tests plus the dialect/port/label/capability deltas.
"""

import pytest

from trove.core.errors import DatasourceError
from trove.services.datasource.adapters.doris import DEFAULT_PORT, DorisAdapter


class FakeCursor:
    def __init__(self, responses=None, description=None):
        self._responses = list(responses or [])
        self.description = description
        self.executed = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))

    async def fetchall(self):
        return self._responses.pop(0) if self._responses else []

    async def fetchone(self):
        return self._responses.pop(0) if self._responses else None

    async def close(self):
        pass


class FakeConn:
    def __init__(self, cursor_specs=None):
        # cursor_specs: list of (responses, description) per cursor creation
        self._cursor_specs = list(cursor_specs or [])
        self.cursors = []
        self.ping_count = 0

    async def cursor(self):
        spec = self._cursor_specs.pop(0) if self._cursor_specs else ({}, None)
        responses, description = spec
        cur = FakeCursor(responses, description)
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


def make_adapter(monkeypatch, driver=None, config=None):
    driver = driver or FakeDriver()
    monkeypatch.setattr(DorisAdapter, "_get_driver", staticmethod(lambda: driver))
    adapter = DorisAdapter(
        name="test",
        config=config or {
            "host": "127.0.0.1",
            "user": "root", "password": "p", "database": "testdb",
        },
    )
    return adapter, driver


class TestDorisAdapter:
    async def test_dialect_static(self):
        assert DorisAdapter.dialect() == "doris"

    async def test_default_name_and_port(self):
        assert DorisAdapter().name == "doris"
        assert DEFAULT_PORT == 9030

    async def test_connect_uses_doris_default_port(self, monkeypatch):
        adapter, driver = make_adapter(monkeypatch)
        await adapter.connect()
        assert driver.connect_kwargs["port"] == 9030
        assert driver.connect_kwargs["db"] == "testdb"
        assert adapter.is_connected

    async def test_connect_honors_explicit_port(self, monkeypatch):
        adapter, driver = make_adapter(
            monkeypatch, config={
                "host": "127.0.0.1", "port": 9031,
                "user": "u", "password": "p", "database": "db",
            },
        )
        await adapter.connect()
        assert driver.connect_kwargs["port"] == 9031

    async def test_connect_failure_wraps_datasource_error(self, monkeypatch):
        class BadDriver:
            async def connect(self, **kwargs):
                raise OSError("connection refused")

        monkeypatch.setattr(DorisAdapter, "_get_driver", staticmethod(lambda: BadDriver()))
        adapter = DorisAdapter(name="test", config={})
        with pytest.raises(DatasourceError) as exc_info:
            await adapter.connect()
        assert "Doris" in str(exc_info.value)
        assert not adapter.is_connected

    async def test_ping_probe_uses_select_one_not_com_ping(self, monkeypatch):
        """Doris FE 缺 COM_PING — 存活探测走 SELECT 1。"""
        conn = FakeConn()
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()
        await adapter.execute("SELECT * FROM students")
        executed = [sql for cur in conn.cursors for sql, _ in cur.executed]
        assert any(sql == "SELECT 1" for sql in executed)

    async def test_get_schema_via_information_schema(self, monkeypatch):
        conn = FakeConn([
            # connect → SELECT VERSION() (fetchone)
            ([("3.0.1",)], None),
            # get_schema 前置 _ping_reconnect → SELECT 1 探测 (fetchall → [])
            ([], None),
            # get_schema 复用同一 cursor:先 TABLES(fetchall→行列表),再 COLUMNS
            ([
                [("students", 1000)],
                [("id", "int", "NO", "PRI"), ("name", "varchar(64)", "YES", "")],
            ], None),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        schema = await adapter.get_schema()
        assert len(schema.tables) == 1
        table = schema.tables[0]
        assert table.name == "students"
        assert table.row_count_estimate == 1000
        id_col = next(c for c in table.columns if c.name == "id")
        assert id_col.primary_key is True
        name_col = next(c for c in table.columns if c.name == "name")
        assert name_col.primary_key is False

    async def test_get_capabilities(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        caps = await adapter.get_capabilities()
        assert caps.dialect == "doris"
        assert caps.supports_cte is True
        assert caps.supports_window_functions is True
        assert caps.supports_transactions is False
        assert caps.supports_json_type is True

    async def test_missing_driver_hint(self, monkeypatch):
        def _missing():
            raise DatasourceError(
                message="aiomysql is not installed — run `uv sync --extra doris`",
                datasource="",
            )

        monkeypatch.setattr(DorisAdapter, "_get_driver", staticmethod(_missing))
        adapter = DorisAdapter(name="test", config={})
        with pytest.raises(DatasourceError) as exc_info:
            await adapter.connect()
        assert "uv sync --extra doris" in str(exc_info.value)
