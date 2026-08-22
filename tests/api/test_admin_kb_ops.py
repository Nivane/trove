"""Admin KB init/reload endpoint tests (Task 5)."""

from __future__ import annotations


async def test_kb_init_mutex(client, api_app):
    """Spec §4 防重入:init 进行中重复请求 → 409。慢 LLM mock 保持 in-flight。

    注册走 registry 直注且非默认(T3 遗留:API 新建 URL 数据源必成默认,
    init_kb 的 get_schema() 会解析到空 schema→零 LLM 调用→init 瞬时完成,
    互斥从未被触发);SlowLLM 返回可解析的 TABLES_DOC 使 init 真走到 200。
    """
    import asyncio
    from trove.core.types import DatasourceConfig
    from tests.cli.test_kb_commands import TABLES_DOC

    await api_app.state.connector_registry.register(
        DatasourceConfig(name="extra", type="sqlite",
                         connection_params={"path": ":memory:"}, credentials={}, default=False))

    class SlowLLM:
        async def chat(self, *a, **k):
            await asyncio.sleep(0.2)
            return TABLES_DOC

        async def chat_full(self, *a, **k):
            await asyncio.sleep(0.2)
            return {"content": TABLES_DOC, "usage": None}

    api_app.state.llm_gateway = SlowLLM()  # init_pipeline 只调 chat/chat_full（ScriptedGateway 同接口）
    async with asyncio.TaskGroup() as tg:
        t1 = tg.create_task(client.post("/v1/admin/datasources/extra/kb/init", json={}))
        await asyncio.sleep(0.02)  # 保证第一个请求已进入管线
        t2 = tg.create_task(client.post("/v1/admin/datasources/extra/kb/init", json={}))
    assert {t1.result().status_code, t2.result().status_code} == {200, 409}


async def test_kb_init_endpoint(client, api_app, tmp_path):
    await client.post("/v1/admin/datasources",
                      json={"name": "extra", "url": "sqlite://:memory:"})
    resp = await client.post("/v1/admin/datasources/extra/kb/init", json={})
    assert resp.status_code == 200
    assert "Initialized" in resp.json()["summary"]
    kb = api_app.state.kb
    assert (kb.kb_dir / "extra" / "semantics.yml").exists()


async def test_kb_reload_endpoint(client, api_app, tmp_path):
    await client.post("/v1/admin/datasources",
                      json={"name": "extra", "url": "sqlite://:memory:"})
    resp = await client.post("/v1/admin/datasources/extra/kb/reload")
    assert resp.status_code == 200
    status = resp.json()["status"]
    assert all(k in status for k in ("initialized", "files", "items"))


async def test_kb_init_unknown_ds_404(client):
    resp = await client.post("/v1/admin/datasources/nope/kb/init", json={})
    assert resp.status_code == 404
