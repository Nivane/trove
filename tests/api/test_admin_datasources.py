"""Admin datasource CRUD endpoints — register (connect-probed) / list /
delete (KB files kept) / reconnect. All writes go through the audit log."""

from __future__ import annotations


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


async def test_register_demo(client, api_app):
    resp = await client.post("/v1/admin/datasources", json={"name": "demo"})
    assert resp.status_code == 201
    assert api_app.state.connector_registry.is_registered("demo")


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
