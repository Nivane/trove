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
        ds_id=kw.get("ds_id", ""),
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


def test_roundtrip_retrieval_tuning_fields(tmp_path):
    """混合检索调优字段(embedder/sparse/rrf/rerank)必须 survive save→load。"""
    store = ConfigStore(tmp_path / "datasources.yml")
    cfg = DatasourceConfig(
        name="r", type="postgres", retrieval_dsn="postgresql://x",
        embedder_backend="bge-m3", embedding_model="BAAI/bge-m3",
        embedding_sparse_dims=250000, rrf_k=100,
        rrf_weights={"keyword": 1.5, "dense": 1.0, "sparse": 0.7},
        rerank_backend="bge", rerank_endpoint="https://x/rerank",
    )
    store.save_configs([cfg])
    loaded = store.load_configs()[0]
    assert loaded.embedder_backend == "bge-m3"
    assert loaded.embedding_sparse_dims == 250000
    assert loaded.rrf_k == 100
    assert loaded.rrf_weights == {"keyword": 1.5, "dense": 1.0, "sparse": 0.7}
    assert loaded.rerank_backend == "bge"
    assert loaded.rerank_endpoint == "https://x/rerank"


def test_roundtrip_retrieval_tuning_defaults(tmp_path):
    """缺省时保持默认值(旧 yml 向后兼容)。"""
    store = ConfigStore(tmp_path / "datasources.yml")
    store.save_configs([_cfg("a")])
    loaded = store.load_configs()[0]
    assert loaded.embedder_backend == ""
    assert loaded.embedding_sparse_dims == 0
    assert loaded.rrf_k == 60
    assert loaded.rrf_weights == {}
    assert loaded.rerank_backend == ""


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


def test_ds_id_persisted_and_backfilled(tmp_path):
    """新写落 id;旧 yml 无 id 用确定性 uuid5 回填,重载幂等。"""
    store = ConfigStore(tmp_path / "datasources.yml")
    store.save_configs([_cfg("a")])
    persisted = (tmp_path / "datasources.yml").read_text(encoding="utf-8")
    assert "id:" in persisted
    loaded = store.load_configs()
    assert loaded[0].ds_id

    # 去掉 id 字段(模拟旧格式文件)→ 确定性回填且稳定
    legacy = """datasources:\n- name: a\n  type: sqlite\n  connection:\n    path: /tmp/x.db\n"""
    (tmp_path / "datasources.yml").write_text(legacy, encoding="utf-8")
    first = store.load_configs()[0].ds_id
    second = store.load_configs()[0].ds_id
    assert first == second and len(first) >= 16


def test_load_duplicate_ds_id_rejected(tmp_path):
    """唯一性约束落在持久层:重复 ds_id 判为损坏,启动 fail-fast 不静默覆盖。"""
    store = ConfigStore(tmp_path / "datasources.yml")
    (tmp_path / "datasources.yml").write_text(
        "datasources:\n"
        "- name: a\n  type: sqlite\n  connection:\n    path: /tmp/x.db\n"
        "  id: same-id\n"
        "- name: b\n  type: sqlite\n  connection:\n    path: /tmp/y.db\n"
        "  id: same-id\n",
        encoding="utf-8",
    )
    with pytest.raises(DatasourceError, match="duplicate datasource id"):
        store.load_configs()


def test_save_duplicate_ds_id_rejected(tmp_path):
    store = ConfigStore(tmp_path / "datasources.yml")
    with pytest.raises(DatasourceError, match="duplicate datasource id"):
        store.save_configs([_cfg("a", ds_id="same"), _cfg("b", ds_id="same")])
