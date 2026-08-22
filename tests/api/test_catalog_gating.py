"""Catalog gating tests: non-admin sees only granted ∧ initialized datasources."""

from __future__ import annotations

from trove.core.types import DatasourceConfig


async def test_user_only_sees_initialized_and_granted(user_client, api_app, api_kb, auth_service):
    """api_kb seeds test_db's KB (init'd); grant it to bob; another
    registered-but-uninitialized ds stays hidden."""
    bob = await auth_service.authenticate("bob", "bobpw")  # returns public user dict with "id"
    await auth_service.set_datasources(bob["id"], ["test_db"])
    body = (await user_client.get("/v1/catalog/datasources")).json()
    names = {d["name"] for d in body["datasources"]}
    assert names == {"test_db"}
    assert body["datasources"][0]["kb_initialized"] is True

    # 补强:注册一个未 init 的数据源并同时 grant 两者——只有 init'd 的 test_db 可见
    await api_app.state.connector_registry.register(
        DatasourceConfig(name="extra", type="sqlite",
                         connection_params={"path": ":memory:"}, credentials={}, default=False))
    await auth_service.set_datasources(bob["id"], ["test_db", "extra"])
    body = (await user_client.get("/v1/catalog/datasources")).json()
    assert {d["name"] for d in body["datasources"]} == {"test_db"}


async def test_admin_sees_all_with_flags(client, api_app):
    await client.post("/v1/admin/datasources",
                      json={"name": "extra", "url": "sqlite://:memory:"})
    body = (await client.get("/v1/catalog/datasources")).json()
    names = {d["name"] for d in body["datasources"]}
    assert "extra" in names and "test_db" in names
    extra = next(d for d in body["datasources"] if d["name"] == "extra")
    assert extra["kb_initialized"] is False
    assert extra["status"] == "connected"
