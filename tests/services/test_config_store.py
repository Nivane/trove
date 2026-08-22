from pathlib import Path
import pytest

from trove.services.datasource.config_store import ConfigStore, boot_register
from trove.core.types import DatasourceConfig
from trove.core.errors import DatasourceError


def _cfg(name="sqlite", **kw):
    return DatasourceConfig(
        name=name, type="sqlite",
        connection_params=kw.get("connection_params", {"path": "/tmp/x.db"}),
        credentials={}, default=kw.get("default", False),
    )


def test_roundtrip(tmp_path):
    store = ConfigStore(tmp_path / "datasources.yml")
    cfgs = [_cfg("a"), _cfg("b", default=True)]
    store.save_configs(cfgs)
    loaded = store.load_configs()
    assert [(c.name, c.type, c.default) for c in loaded] == [("a", "sqlite", False), ("b", "sqlite", True)]
    assert loaded[0].connection_params == {"path": "/tmp/x.db"}


def test_load_missing_file(tmp_path):
    assert ConfigStore(tmp_path / "nope.yml").load_configs() == []


async def test_boot_register_registers_and_skips_bad(sqlite_registry, tmp_path):
    store = ConfigStore(tmp_path / "datasources.yml")
    good = DatasourceConfig(name="good", type="sqlite",
                            connection_params={"path": ":memory:"}, credentials={}, default=True)
    bad = DatasourceConfig(name="bad", type="mysql",
                           connection_params={"host": "127.0.0.1", "port": 1, "user": "u",
                                              "password": "p", "database": "d"},
                           credentials={}, default=False)
    failed = await boot_register(sqlite_registry, [good, bad])
    assert failed == ["bad"]
    assert sqlite_registry.is_registered("good")
    assert not sqlite_registry.is_registered("bad")
