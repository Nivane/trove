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


async def test_register_with_retrieval_backend(client, api_app):
    """注册时可选 retrieval_backend 字段:持久化 + 列表暴露 + 非法值 400。"""
    resp = await client.post(
        "/v1/admin/datasources",
        json={"name": "hybriddb", "url": "sqlite://:memory:",
              "retrieval_backend": "hybrid"},
    )
    assert resp.status_code == 201
    persisted = next(
        c for c in api_app.state.config_store.load_configs()
        if c.name == "hybriddb")
    assert persisted.retrieval_backend == "hybrid"

    listed = (await client.get("/v1/admin/datasources")).json()["datasources"]
    entry = next(d for d in listed if d["name"] == "hybriddb")
    assert entry["retrieval_backend"] == "hybrid"

    bad = await client.post(
        "/v1/admin/datasources",
        json={"name": "baddb", "url": "sqlite://:memory:",
              "retrieval_backend": "nope"},
    )
    assert bad.status_code == 400

    # 缺省 → builtin
    plain = await client.post(
        "/v1/admin/datasources",
        json={"name": "plaindb", "url": "sqlite://:memory:"},
    )
    assert plain.status_code == 201
    assert plain.json()["datasource"]["retrieval_backend"] == "builtin"


async def test_register_rag_requires_embedding_model(client, api_app):
    """rag 后端必须配 embedding_model;pgvector 必须配 vector_dsn。"""
    no_emb = await client.post(
        "/v1/admin/datasources",
        json={"name": "ragdb", "url": "sqlite://:memory:",
              "retrieval_backend": "rag"},
    )
    assert no_emb.status_code == 400

    ok = await client.post(
        "/v1/admin/datasources",
        json={"name": "ragdb", "url": "sqlite://:memory:",
              "retrieval_backend": "rag", "embedding_model": "bge-m3"},
    )
    assert ok.status_code == 201
    entry = ok.json()["datasource"]
    assert entry["embedding_model"] == "bge-m3"
    assert entry["vector_backend"] == "sqlite"

    no_dsn = await client.post(
        "/v1/admin/datasources",
        json={"name": "vecragdb", "url": "sqlite://:memory:",
              "retrieval_backend": "rag", "embedding_model": "bge-m3",
              "vector_backend": "pgvector"},
    )
    assert no_dsn.status_code == 400

    bad_vb = await client.post(
        "/v1/admin/datasources",
        json={"name": "vecragdb", "url": "sqlite://:memory:",
              "retrieval_backend": "rag", "embedding_model": "bge-m3",
              "vector_backend": "chroma", "vector_dsn": "postgresql://x"},
    )
    assert bad_vb.status_code == 400


async def test_list_without_kb_mirror(client, api_app):
    """KB 目录存在但 kb.sqlite 镜像未建（挂载 .trove 的真实生产形态）→ 列表不 500，
    kb_initialized 仍正确（从 YAML 文件判定，不依赖镜像）。"""
    kb = api_app.state.kb
    ds_dir = kb.kb_dir / "test_db"
    ds_dir.mkdir(parents=True, exist_ok=True)
    # 完整初始化 = 三个关键文件齐全(schema_notes/semantics/examples)
    for name, content in {
        "schema_notes.yml": "tables: []\n",
        "semantics.yml": "semantic_model: []\n",
        "examples.yml": "examples: []\n",
    }.items():
        (ds_dir / name).write_text(content, encoding="utf-8")
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

    await fresh.close_all()


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


# ── 编辑(detail 预填 / PUT 更新 / 测试连接) ─────────────────


async def test_detail_returns_url_for_edit(client, api_app):
    await client.post("/v1/admin/datasources",
                      json={"name": "extra", "url": "sqlite://:memory:"})
    resp = await client.get("/v1/admin/datasources/extra")
    assert resp.status_code == 200
    ds = resp.json()["datasource"]
    assert ds["name"] == "extra"
    assert ds["url"] == "sqlite://:memory:"
    assert ds["status"] == "connected"
    assert ds["kb_initialized"] is False

    # 持久层含凭据的源：URL 重建保留凭据(admin-only)。
    from trove.core.types import DatasourceConfig
    api_app.state.config_store.save_configs([
        DatasourceConfig(name="dbx", type="mysql",
                         connection_params={
                             "host": "h.example", "port": 3306,
                             "user": "root", "password": "s3cret",
                             "database": "appdb"},
                         credentials={}, default=False, ds_id="id-dbx"),
    ])
    resp = await client.get("/v1/admin/datasources/dbx")
    ds = resp.json()["datasource"]
    assert ds["url"] == "mysql://root:s3cret@h.example:3306/appdb"
    assert ds["status"] == "disconnected"


async def test_detail_unknown_404(client):
    assert (await client.get("/v1/admin/datasources/ghost")).status_code == 404


async def test_update_datasource(client, api_app, tmp_path):
    resp = await client.post("/v1/admin/datasources",
                             json={"name": "extra", "url": "sqlite://:memory:"})
    assert resp.status_code == 201
    ds_id = resp.json()["datasource"]["id"]

    file_db = tmp_path / "moved.db"
    resp = await client.put("/v1/admin/datasources/extra",
                            json={"url": f"sqlite://{file_db}"})
    assert resp.status_code == 200
    assert resp.json()["datasource"]["id"] == ds_id  # 身份不变
    assert api_app.state.connector_registry.is_registered("extra")
    persisted = next(c for c in api_app.state.config_store.load_configs()
                     if c.name == "extra")
    assert persisted.connection_params["path"] == str(file_db)


async def test_update_bad_url_400(client):
    await client.post("/v1/admin/datasources",
                      json={"name": "extra", "url": "sqlite://:memory:"})
    resp = await client.put("/v1/admin/datasources/extra", json={"url": "bogus://nope"})
    assert resp.status_code == 400
    assert "Unsupported datasource scheme 'bogus'" in resp.json()["detail"]


async def test_update_unknown_404(client):
    assert (await client.put("/v1/admin/datasources/ghost",
                             json={"url": "sqlite://:memory:"})).status_code == 404


async def test_update_missing_url_400(client):
    await client.post("/v1/admin/datasources",
                      json={"name": "extra", "url": "sqlite://:memory:"})
    assert (await client.put("/v1/admin/datasources/extra", json={})).status_code == 400


async def test_update_blocked_when_kb_initialized(client, api_app):
    """KB 初始化后数据源锁定：编辑被拒(409)，配置与连接不变。"""
    await client.post("/v1/admin/datasources",
                      json={"name": "extra", "url": "sqlite://:memory:"})
    kb = api_app.state.kb
    ds_dir = kb.kb_dir / "extra"
    ds_dir.mkdir(parents=True, exist_ok=True)
    for name, content in {
        "schema_notes.yml": "tables: []\n",
        "semantics.yml": "semantic_model: []\n",
        "examples.yml": "examples: []\n",
    }.items():
        (ds_dir / name).write_text(content, encoding="utf-8")

    resp = await client.put("/v1/admin/datasources/extra",
                            json={"url": "sqlite://:memory:"})
    assert resp.status_code == 409
    assert "knowledge base" in resp.json()["detail"]


async def test_update_demo_400(client):
    await client.post("/v1/admin/datasources", json={"name": "demo"})
    resp = await client.put("/v1/admin/datasources/demo",
                            json={"url": "sqlite://:memory:"})
    assert resp.status_code == 400
    assert "demo" in resp.json()["detail"].lower()


async def test_test_connection_by_url(client):
    ok = (await client.post("/v1/admin/datasources/test-connection",
                            json={"url": "sqlite://:memory:"})).json()
    assert ok["ok"] is True and ok["error"] is None
    bad = (await client.post("/v1/admin/datasources/test-connection",
                             json={"url": "bogus://nope"})).json()
    assert bad["ok"] is False and "Unsupported datasource scheme 'bogus'" in bad["error"]
    probe_fail = (await client.post("/v1/admin/datasources/test-connection",
                                    json={"url": "sqlite:///no/such/dir_xyz/x.db"})).json()
    assert probe_fail["ok"] is False


async def test_test_connection_by_name(client):
    await client.post("/v1/admin/datasources",
                      json={"name": "extra", "url": "sqlite://:memory:"})
    ok = (await client.post("/v1/admin/datasources/test-connection",
                            json={"name": "extra"})).json()
    assert ok["ok"] is True
    missing = (await client.post("/v1/admin/datasources/test-connection",
                                 json={"name": "ghost"})).json()
    assert missing["ok"] is False


async def test_test_connection_demo(client):
    ok = (await client.post("/v1/admin/datasources/test-connection",
                            json={"url": "demo"})).json()
    assert ok["ok"] is True


async def test_test_connection_requires_input(client):
    assert (await client.post("/v1/admin/datasources/test-connection",
                              json={})).status_code == 400


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


class TestVectorConfigDefault:
    """默认向量后端按业务库类型:postgres → pgvector(同实例),其它 → sqlite。"""

    def _vc(self, body, ds_type=""):
        from trove.api.routers.admin import _vector_config

        return _vector_config(body, ds_type)

    def test_postgres_defaults_pgvector(self):
        out = self._vc({"retrieval_backend": "rag", "embedding_model": "bge-m3"},
                       ds_type="postgres")
        assert out["vector_backend"] == "pgvector"
        assert out["vector_dsn"] == ""  # 同实例推导,无需显式 dsn

    def test_non_postgres_defaults_sqlite(self):
        out = self._vc({"retrieval_backend": "rag", "embedding_model": "bge-m3"},
                       ds_type="sqlite")
        assert out["vector_backend"] == "sqlite"

    def test_explicit_vector_backend_wins(self):
        out = self._vc({"vector_backend": "sqlite"}, ds_type="postgres")
        assert out["vector_backend"] == "sqlite"

    def test_pgvector_requires_dsn_on_non_postgres(self):
        import pytest as _pytest
        from fastapi import HTTPException

        with _pytest.raises(HTTPException) as exc:
            self._vc({"vector_backend": "pgvector"}, ds_type="mysql")
        assert exc.value.status_code == 400

    def test_pgvector_ok_without_dsn_on_postgres(self):
        out = self._vc({"vector_backend": "pgvector"}, ds_type="postgres")
        assert out["vector_backend"] == "pgvector"
