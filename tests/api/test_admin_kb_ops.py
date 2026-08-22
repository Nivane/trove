"""Admin KB init/reload endpoint tests (Task 5)."""

from __future__ import annotations

# employees 单表草稿(与 TABLES_DOC 同构)——I1-T5 错源回归专用:
# 默认源的 conftest mock 只草拟 students,无法覆盖 employees schema
# (修复轮耗尽 400),故此处用专用 mock 让修复后的管线走到 200。
EMPLOYEES_DOC = """tables:
- name: employees
  description: employee records
  columns:
  - name: id
    type: int
    description: employee identifier
    enums: []
  - name: name
    type: text
    description: employee name
    enums: []
  metrics: []
"""


async def test_kb_init_mutex(client, api_app, tmp_path):
    """Spec §4 防重入:init 进行中重复请求 → 409。慢 LLM mock 保持 in-flight。

    注册走 registry 直注且非默认(T3 遗留:API 新建 URL 数据源必成默认;
    I1-T5 修后 init_kb 读 extra 自己的 schema——空库会零 LLM 调用、
    init 瞬时完成,互斥从未被触发),故 extra 用文件 sqlite 且带一张
    employees 表;SlowLLM 返回与之一致的 EMPLOYEES_DOC 使 init 真走到 200。
    """
    import asyncio
    import sqlite3
    from trove.core.types import DatasourceConfig

    db = tmp_path / "extra.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()
    await api_app.state.connector_registry.register(
        DatasourceConfig(name="extra", type="sqlite",
                         connection_params={"path": str(db)}, credentials={}, default=False))

    class SlowLLM:
        async def chat(self, *a, **k):
            await asyncio.sleep(0.2)
            return EMPLOYEES_DOC

        async def chat_full(self, *a, **k):
            await asyncio.sleep(0.2)
            return {"content": EMPLOYEES_DOC, "usage": None}

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
    # M3: kb/reload 是写镜像的变更操作,必须留审计(同 kb.init 约定)
    entries = await api_app.state.auth.list_audit(action="kb.reload")
    assert any(e["username"] == "admin" and e["details"] == {"name": "extra"}
               for e in entries)


async def test_kb_init_unknown_ds_404(client):
    resp = await client.post("/v1/admin/datasources/nope/kb/init", json={})
    assert resp.status_code == 404


async def test_kb_reload_unknown_ds_404(client):
    resp = await client.post("/v1/admin/datasources/nope/kb/reload")
    assert resp.status_code == 404


async def test_kb_init_uses_datasource_schema(client, api_app, tmp_path):
    """I1-T5 回归:非默认源 init 必须读自己的 schema,不能读到默认源 (test_db) 的。

    修前 init_kb 的 get_schema() 无参 → 解析到默认源 students → 与
    employees 草稿不匹配 → 修复轮耗尽 400(错源必暴露);修后 schema=
    employees 与草稿吻合 → 200,notes 内容级验证:含 employees、绝不含
    students(默认源的表名不能出现在 extra 的 KB 里)。
    """
    import sqlite3
    from trove.core.types import DatasourceConfig
    from trove.llm.gateway import LLMGateway

    db = tmp_path / "extra.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()
    # 直注非默认:API 新建源必成默认(T3 minor),会把错源 bug 掩盖掉
    await api_app.state.connector_registry.register(
        DatasourceConfig(name="extra", type="sqlite",
                         connection_params={"path": str(db)}, credentials={}, default=False))
    api_app.state.llm_gateway = LLMGateway(mock_response=EMPLOYEES_DOC)

    resp = await client.post("/v1/admin/datasources/extra/kb/init", json={})
    assert resp.status_code == 200, resp.text
    notes = (api_app.state.kb.kb_dir / "extra" / "schema_notes.yml").read_text(encoding="utf-8")
    assert "employees" in notes
    assert "students" not in notes
