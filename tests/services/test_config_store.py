from pathlib import Path
import pytest

from trove.services.datasource.config_store import ConfigStore, boot_register
from trove.core.types import DatasourceConfig
from trove.core.errors import DatasourceError


def _cfg(name="sqlite", **kw):
    return DatasourceConfig(
        name=name, type="sqlite",
        connection_params=kw.get("connection_params", {"path": "/tmp/x.db"}),
        credentials=kw.get("credentials", {}), default=kw.get("default", False),
    )


def test_roundtrip(tmp_path):
    store = ConfigStore(tmp_path / "datasources.yml")
    cfgs = [
        _cfg("a", credentials={"user": "u1", "password": "p1"}),
        _cfg("b", default=True),
    ]
    store.save_configs(cfgs)
    loaded = store.load_configs()
    assert [(c.name, c.type, c.default) for c in loaded] == [("a", "sqlite", False), ("b", "sqlite", True)]
    assert loaded[0].connection_params == {"path": "/tmp/x.db"}
    # credentials 与 default 必须完整 survive save→load(重启恢复依赖)
    assert loaded[0].credentials == {"user": "u1", "password": "p1"}
    assert loaded[1].credentials == {}
    assert loaded[1].default is True


def test_load_top_level_not_mapping(tmp_path):
    """M1: 顶层为列表(合法 YAML、错误形状)必须抛 DatasourceError,不能裸 AttributeError。"""
    store = ConfigStore(tmp_path / "datasources.yml")
    (tmp_path / "datasources.yml").write_text(
        "- name: a\n  type: sqlite\n  connection:\n    path: /tmp/x.db\n",
        encoding="utf-8",
    )
    with pytest.raises(DatasourceError, match="top-level must be a mapping"):
        store.load_configs()


def test_load_datasources_mapping_form(tmp_path):
    """M1: datasources: 为映射形式必须抛 DatasourceError,不能裸 TypeError。"""
    store = ConfigStore(tmp_path / "datasources.yml")
    (tmp_path / "datasources.yml").write_text(
        "datasources:\n  financial:\n    type: sqlite\n",
        encoding="utf-8",
    )
    with pytest.raises(DatasourceError, match="'datasources' must be a list"):
        store.load_configs()


def test_load_datasources_entry_not_mapping(tmp_path):
    """终审 residual: 列表内非 dict 条目(- foo)必须抛 DatasourceError,不能裸 TypeError。"""
    store = ConfigStore(tmp_path / "datasources.yml")
    (tmp_path / "datasources.yml").write_text(
        "datasources:\n  - foo\n",
        encoding="utf-8",
    )
    with pytest.raises(DatasourceError, match="expected a mapping"):
        store.load_configs()


def test_save_leaves_no_temp_files(tmp_path):
    """M2: 原子写——save 后同目录不得残留临时文件。"""
    store = ConfigStore(tmp_path / "datasources.yml")
    store.save_configs([_cfg("a"), _cfg("b", default=True)])
    store.save_configs([_cfg("a")])  # 二次覆盖走同一路径
    assert list(tmp_path.glob("*.tmp")) == []
    assert (tmp_path / "datasources.yml").exists()


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
