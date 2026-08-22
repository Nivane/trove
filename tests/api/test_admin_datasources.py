"""Admin datasource CRUD endpoints — register (connect-probed) / list /
delete (KB files kept) / reconnect / naming rules / 409 conflicts /
transactional rollback. All writes go through the audit log."""

from __future__ import annotations

import pytest


async def test_register_list_delete(client, api_app, tmp_path):
    resp = await client.post("/v1/admin/datasources",
                             json={"name": "extra", "url": "sqlite://:memory:"})
    assert resp.status_code == 201
    assert api_app.state.connector_registry.is_registered("extra")
    assert api_app.state.config_store.path.exists()

    listed = (await client.get("/v1/admin/datasources")).json()["datasources"]
    names = {d["name"] for d in listed}
    assert "extra" in names
    extra = next(d for d in listed if d["name"] == "extra")
    assert extra["status"] == "connected"
    assert "kb_initialized" in extra

    assert (await client.delete("/v1/admin/datasources/extra")).status_code == 204
    assert not api_app.state.connector_registry.is_registered("extra")
    # DELETE 删除持久化记录：列表不再出现（KB 文件保留，可复用）
    listed_after = (await client.get("/v1/admin/datasources")).json()["datasources"]
    assert "extra" not in {d["name"] for d in listed_after}
    assert "extra" not in {c.name for c in api_app.state.config_store.load_configs()}


async def test_list_without_kb_mirror(client, api_app):
    """KB 目录存在但 kb.sqlite 镜像未建（挂载 .trove 的真实生产形态）→ 列表不 500，
    kb_initialized 仍正确（从 YAML 文件判定，不依赖镜像）。"""
    kb = api_app.state.kb
    ds_dir = kb.kb_dir / "test_db"
    ds_dir.mkdir(parents=True, exist_ok=True)
    (ds_dir / "schema_notes.yml").write_text("tables: []\n", encoding="utf-8")
    if kb.db_path.exists():  # api_app fixture 已 ensure_synced，先删镜像再测
        kb.db_path.unlink()

    resp = await client.get("/v1/admin/datasources")
    assert resp.status_code == 200
    test_db = next(d for d in resp.json()["datasources"] if d["name"] == "test_db")
    assert test_db["kb_initialized"] is True


async def test_register_bad_url_400(client):
    resp = await client.post("/v1/admin/datasources",
                             json={"name": "x", "url": "bogus://nope"})
    assert resp.status_code == 400
    # detail 必须带具体原因（spec §4），不是笼统的失败
    assert "Unsupported datasource scheme 'bogus'" in resp.json()["detail"]


async def test_register_probe_failure_400(client, api_app):
    """URL 合法但连接探测失败（sqlite 父目录不存在）→ 400 且 detail 报探测原因，
    注册不落地（registry 无残留、datasources.yml 不写入）。"""
    resp = await client.post("/v1/admin/datasources",
                             json={"name": "probe", "url": "sqlite:///no/such/dir_xyz/x.db"})
    assert resp.status_code == 400
    assert "Failed to connect to SQLite" in resp.json()["detail"]
    assert not api_app.state.connector_registry.is_registered("probe")
    assert "probe" not in {c.name for c in api_app.state.config_store.load_configs()}


async def test_register_demo(client, api_app):
    resp = await client.post("/v1/admin/datasources", json={"name": "demo"})
    assert resp.status_code == 201
    assert api_app.state.connector_registry.is_registered("demo")


async def test_register_demo_default_persisted(client, api_app):
    """T8 demo 默认评估顺序修复：注册前先取默认态，持久化的 default 与注册结果一致。

    修前 setup_demo_datasource 内部固定 set_default=True，随后
    `default=registry.default_name is None` 恒为 False → yml 记录 default=False
    而 registry 里 demo 是默认，重启后默认位丢失。现修复：无默认时注册 demo →
    default=True；已有默认（extra）时注册 demo → default=False 且不抢默认位。
    """
    from trove.core.types import DatasourceConfig
    from trove.services.datasource.registry import ConnectorRegistry

    fresh = ConnectorRegistry()
    api_app.state.connector_registry = fresh

    # 1) 空默认态：demo 成为默认并持久化 default=True
    resp = await client.post("/v1/admin/datasources", json={"name": "demo"})
    assert resp.status_code == 201
    assert resp.json()["datasource"]["default"] is True
    assert fresh.default_name == "demo"
    demo_cfg = next(c for c in api_app.state.config_store.load_configs()
                    if c.name == "demo")
    assert demo_cfg.default is True

    # 2) 已有默认（extra）时再注册 demo：不抢默认位，持久化 default=False
    await fresh.register(
        DatasourceConfig(name="extra", type="sqlite",
                         connection_params={"path": ":memory:"}, credentials={},
                         default=True), set_default=True)
    assert fresh.default_name == "extra"
    resp = await client.post("/v1/admin/datasources", json={"name": "demo"})
    assert resp.status_code == 201
    assert resp.json()["datasource"]["default"] is False
    assert fresh.default_name == "extra"
    demo_cfg = next(c for c in api_app.state.config_store.load_configs()
                    if c.name == "demo")
    assert demo_cfg.default is False


async def test_non_admin_403(user_client):
    assert (await user_client.post("/v1/admin/datasources",
                                   json={"name": "x", "url": "sqlite://:memory:"})).status_code == 403


async def test_reconnect(client, api_app):
    # 模拟启动失败遗留：config 在 datasources.yml 里但未注册（断开态）——
    # boot_register 失败时 config 文件还在而 registry 没有，reconnect 从 config 恢复
    from trove.core.types import DatasourceConfig

    api_app.state.config_store.save_configs([
        DatasourceConfig(name="extra", type="sqlite",
                         connection_params={"path": ":memory:"}, credentials={},
                         default=False),
    ])
    assert not api_app.state.connector_registry.is_registered("extra")
    listed = (await client.get("/v1/admin/datasources")).json()["datasources"]
    extra = next(d for d in listed if d["name"] == "extra")
    assert extra["status"] == "disconnected"

    resp = await client.post("/v1/admin/datasources/extra/reconnect")
    assert resp.status_code == 200
    assert api_app.state.connector_registry.is_registered("extra")


async def test_reconnect_unknown_404(client, api_app):
    assert (await client.post("/v1/admin/datasources/ghost/reconnect")).status_code == 404


# ── ds_id 身份 / 命名规则 / 冲突 409 / 事务性注册 ────────────────


async def test_register_exposes_immutable_id(client, api_app):
    resp = await client.post("/v1/admin/datasources",
                             json={"name": "extra", "url": "sqlite://:memory:"})
    assert resp.status_code == 201
    ds_id = resp.json()["datasource"]["id"]
    assert isinstance(ds_id, str) and len(ds_id) >= 16
    assert api_app.state.connector_registry.identity_of("extra") == ds_id
    persisted = next(c for c in api_app.state.config_store.load_configs()
                     if c.name == "extra")
    assert persisted.ds_id == ds_id
    # 幂等重注册:同一身份,持久层条目不翻倍
    again = await client.post("/v1/admin/datasources",
                              json={"name": "extra", "url": "sqlite://:memory:"})
    assert again.status_code == 201
    assert again.json()["datasource"]["id"] == ds_id
    entries = [c.name for c in api_app.state.config_store.load_configs()]
    assert entries.count("extra") == 1


async def test_register_invalid_name_400(client):
    for bad in ("My DB", "ab", "bad/name", "has.dot", "UPPER"):
        resp = await client.post("/v1/admin/datasources",
                                 json={"name": bad, "url": "sqlite://:memory:"})
        assert resp.status_code == 400, f"{bad} should be rejected"
        assert "invalid datasource name" in resp.json()["detail"]


async def test_register_reserved_name_400(client):
    resp = await client.post("/v1/admin/datasources",
                             json={"name": "default", "url": "sqlite://:memory:"})
    assert resp.status_code == 400
    assert "reserved" in resp.json()["detail"]


async def test_url_derived_name_bypasses_slug_rule(api_app):
    """URL 派生名字(如 mysql 库名 some_db,带下划线)只走 path-safety,不被 slug 卡死。"""
    from trove.services.datasource.urls import parse_datasource_url

    cfg = api_app.state.connector_registry.ensure_identity(
        parse_datasource_url("mysql://root@localhost/some_db"))
    assert cfg.name == "some_db"
    assert cfg.ds_id  # 身份已生成
    # slug 规则只约束显式输入;显式输入 same name 反而被拒
    from trove.services.datasource.naming import validate_datasource_name
    with pytest.raises(Exception):
        validate_datasource_name("some_db")


async def test_register_same_name_different_identity_409(client, api_app):
    """同名不同身份 → 409,绝不静默覆盖;registry 层同样守卫。"""
    from trove.core.errors import DatasourceConflictError
    from trove.core.types import DatasourceConfig

    resp = await client.post("/v1/admin/datasources",
                             json={"name": "extra", "url": "sqlite://:memory:"})
    assert resp.status_code == 201
    registry = api_app.state.connector_registry

    # registry 层:显式不同 ds_id 注册同名 → conflict
    with pytest.raises(DatasourceConflictError):
        await registry.register(DatasourceConfig(
            name="extra", type="sqlite",
            connection_params={"path": ":memory:"}, credentials={},
            default=False, ds_id="other-identity"))

    # API 层:持久层把同名绑到另一个身份(外部篡改/双写),再注册 → 409
    api_app.state.config_store.save_configs([
        DatasourceConfig(name="extra", type="sqlite",
                         connection_params={"path": ":memory:"}, credentials={},
                         default=False, ds_id="other-identity"),
    ])
    resp = await client.post("/v1/admin/datasources",
                             json={"name": "extra", "url": "sqlite://:memory:"})
    assert resp.status_code == 409


async def test_register_unpathsafe_name_rejected(client, api_app):
    from trove.core.errors import DatasourceError
    from trove.core.types import DatasourceConfig

    with pytest.raises(DatasourceError):
        await api_app.state.connector_registry.register(DatasourceConfig(
            name="../escape", type="sqlite",
            connection_params={"path": ":memory:"}, credentials={}, default=False))


async def test_register_persist_failure_rolls_back(client, api_app, tmp_path):
    """事务性注册:连接成功但落库失败 → 回滚,registry 无残留、config 不写入。"""
    from trove.services.datasource.config_store import ConfigStore

    blocker = tmp_path / "block"
    blocker.write_text("x", encoding="utf-8")  # 把一个"文件"顶到父目录位置
    api_app.state.config_store = ConfigStore(blocker / "datasources.yml")

    resp = await client.post("/v1/admin/datasources",
                             json={"name": "txnds", "url": "sqlite://:memory:"})
    assert resp.status_code == 400
    assert "persist" in resp.json()["detail"] or "rolled back" in resp.json()["detail"]
    assert not api_app.state.connector_registry.is_registered("txnds")
