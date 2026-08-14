"""--datasource dispatch tests (demo / scheme:// URLs)."""

from types import SimpleNamespace

import pytest

from trove.core.errors import DatasourceError
from trove.main import setup_datasource


class StubRegistry:
    """Records register() calls without connecting to anything."""

    def __init__(self):
        self.configs = []

    async def register(self, config, set_default=False):
        self.configs.append((config, set_default))
        return None


class TestSetupDatasource:
    async def test_demo_dispatches_to_demo_setup(self, monkeypatch):
        called = {}

        async def fake_demo(registry):
            called["registry"] = registry

        monkeypatch.setattr("trove.main.setup_demo_datasource", fake_demo)
        registry = StubRegistry()
        await setup_datasource(SimpleNamespace(datasource="demo"), registry)
        assert called["registry"] is registry

    async def test_sqlite_url_registers(self):
        registry = StubRegistry()
        await setup_datasource(SimpleNamespace(datasource="sqlite://:memory:"), registry)
        cfg, default = registry.configs[0]
        assert cfg.type == "sqlite"
        assert cfg.connection_params == {"path": ":memory:"}
        assert default is True

    async def test_mysql_url_registers(self):
        registry = StubRegistry()
        await setup_datasource(
            SimpleNamespace(datasource="mysql://root:p@127.0.0.1:3306/apboa"),
            registry,
        )
        cfg, default = registry.configs[0]
        assert cfg.type == "mysql"
        assert cfg.name == "apboa"
        assert cfg.connection_params["host"] == "127.0.0.1"

    async def test_clickhouse_url_registers(self):
        registry = StubRegistry()
        await setup_datasource(
            SimpleNamespace(datasource="clickhouse://default@127.0.0.1:8123/events"),
            registry,
        )
        cfg, _ = registry.configs[0]
        assert cfg.type == "clickhouse"
        assert cfg.name == "events"

    async def test_duckdb_url_registers(self):
        registry = StubRegistry()
        await setup_datasource(
            SimpleNamespace(datasource="duckdb:///tmp/d.duckdb"),
            registry,
        )
        cfg, _ = registry.configs[0]
        assert cfg.type == "duckdb"

    async def test_bad_url_raises(self):
        registry = StubRegistry()
        with pytest.raises(DatasourceError):
            await setup_datasource(SimpleNamespace(datasource="mysql://root@host"), registry)
        assert registry.configs == []

    async def test_unknown_string_raises(self):
        registry = StubRegistry()
        with pytest.raises(DatasourceError):
            await setup_datasource(SimpleNamespace(datasource="oracle-thing"), registry)
