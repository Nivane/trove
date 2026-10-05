"""/v1/admin/settings — DB-backed runtime config (admin-only)."""

from __future__ import annotations

import pytest

from trove.services.admin_settings.service import MASK
from trove.services.admin_settings.store import SettingsStore


@pytest.fixture
async def settings_store():
    """Dispose any SettingsStore the test attached (aiosqlite worker cleanup)."""
    store = []
    yield store
    for s in store:
        await s.dispose()


async def _with_store(api_app, tmp_path, store: list) -> SettingsStore:
    s = SettingsStore(tmp_path / "settings.db")
    api_app.state.settings = s
    store.append(s)
    return s


class TestSettingsApi:
    async def test_get_returns_effective_values(self, client, api_app, tmp_path, settings_store):
        await _with_store(api_app, tmp_path, settings_store)
        r = await client.get("/v1/admin/settings")
        assert r.status_code == 200
        body = r.json()
        values = body["values"]
        assert values["llm.default_model"] == "mock/model"
        assert values["app.language"] == "zh"
        assert values["app.hitl"] is False
        assert isinstance(values["llm.providers"], list)
        assert body["mask"] == MASK

    async def test_put_applies_and_persists(self, client, api_app, tmp_path, settings_store):
        store = await _with_store(api_app, tmp_path, settings_store)
        r = await client.put("/v1/admin/settings", json={"values": {
            "app.hitl": True,
            "app.language": "en",
            "retention.max_sessions_per_user": 40,
        }})
        assert r.status_code == 200
        values = r.json()["values"]
        assert values["app.hitl"] is True
        assert values["app.language"] == "en"
        assert values["retention.max_sessions_per_user"] == 40
        # hot-applied to the live runtime config
        assert api_app.state.config.hitl is True
        assert api_app.state.config.retention.max_sessions_per_user == 40
        # persisted for next boot
        stored = await store.get_all()
        assert stored["app.hitl"] is True
        assert stored["app.language"] == "en"

    async def test_put_invalid_400_with_detail(self, client, api_app, tmp_path, settings_store):
        await _with_store(api_app, tmp_path, settings_store)
        r = await client.put("/v1/admin/settings", json={"values": {
            "app.language": "fr",
            "app.reflect_skip": "always",
            "not.a.setting": 1,
        }})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "language" in detail
        assert "reflect_skip" in detail
        assert "unknown setting" in detail

    async def test_put_result_limits_applied(self, client, api_app, tmp_path, settings_store):
        """结果限制经 settings API 落库 + 热更新镜像到 pipeline 注册表。"""
        from trove.services.limits import get_result_limits

        store = await _with_store(api_app, tmp_path, settings_store)
        r = await client.put("/v1/admin/settings", json={"values": {
            "app.result_display_rows": 80,
            "app.result_max_rows": 2000,
        }})
        assert r.status_code == 200
        values = r.json()["values"]
        assert values["app.result_display_rows"] == 80
        assert values["app.result_max_rows"] == 2000
        assert api_app.state.config.result_display_rows == 80
        assert api_app.state.config.result_max_rows == 2000
        # 热同步:execute_sql/output 节点可读的进程级注册表(供下载/展示截断)
        assert (get_result_limits().display_rows,
                get_result_limits().max_rows) == (80, 2000)
        stored = await store.get_all()
        assert stored["app.result_max_rows"] == 2000
        # 越界 → 400 且不落库
        bad = await client.put("/v1/admin/settings", json={"values": {
            "app.result_max_rows": -1,
        }})
        assert bad.status_code == 400
        assert (await store.get_all()).get("app.result_max_rows") == 2000
        from trove.services.limits import reset_result_limits
        reset_result_limits()

    async def test_put_provider_api_key_masking(self, client, api_app, tmp_path, settings_store):
        store = await _with_store(api_app, tmp_path, settings_store)
        # store a real secret via the API (full value, no mask)
        r = await client.put("/v1/admin/settings", json={"values": {"llm.providers": [
            {"name": "openai", "litellm_params": {"api_key": "sk-super-secret",
                                                  "api_base": "https://api.openai.com"}},
        ]}})
        assert r.status_code == 200
        # GET must NOT leak it — only the mask marker
        got = (await client.get("/v1/admin/settings")).json()
        prov = got["values"]["llm.providers"][0]
        assert prov["has_api_key"] is True
        assert prov["litellm_params"]["api_key"] == MASK
        assert "sk-super-secret" not in str(got)
        # updating the endpoint via the mask keeps the original secret
        r2 = await client.put("/v1/admin/settings", json={"values": {"llm.providers": [
            {"name": "openai", "litellm_params": {"api_key": MASK,
                                                  "api_base": "https://new.base"}},
        ]}})
        assert r2.status_code == 200
        stored = await store.get_all()
        stored_params = stored["llm.providers"][0]["litellm_params"]
        assert stored_params["api_key"] == "sk-super-secret"
        assert stored_params["api_base"] == "https://new.base"

    async def test_put_semantic_layer_path(self, client, api_app, tmp_path, settings_store):
        """语义层目录经 settings API 落库 + 应用到运行时 config。"""
        store = await _with_store(api_app, tmp_path, settings_store)
        r = await client.put("/v1/admin/settings", json={"values": {
            "app.semantic_layer_path": ".trove/semantic",
        }})
        assert r.status_code == 200
        assert r.json()["values"]["app.semantic_layer_path"] == ".trove/semantic"
        assert api_app.state.config.semantic_layer_path == ".trove/semantic"
        stored = await store.get_all()
        assert stored["app.semantic_layer_path"] == ".trove/semantic"
        # 空串 = 关闭,允许
        r2 = await client.put("/v1/admin/settings", json={"values": {
            "app.semantic_layer_path": "",
        }})
        assert r2.status_code == 200
        assert api_app.state.config.semantic_layer_path == ""

    async def test_put_draft_model_applies_and_may_clear(
            self, client, api_app, tmp_path, settings_store):
        """llm.draft_model 是 path 型(A3):可设可清 —— 与 str 型的区别就在
        「允许空串」,因为空 = 关闭起草档 = 全收编点逐字节回落。"""
        await _with_store(api_app, tmp_path, settings_store)
        r = await client.put("/v1/admin/settings", json={"values": {
            "llm.draft_model": "deepseek/deepseek-chat",
        }})
        assert r.status_code == 200
        assert r.json()["values"]["llm.draft_model"] == "deepseek/deepseek-chat"
        # 热应用到运行时 config,且 model_for_draft 当场改道
        assert api_app.state.config.model_draft == "deepseek/deepseek-chat"
        assert api_app.state.config.model_for_draft(
            "kb_init", "standard") == "deepseek/deepseek-chat"
        # 清空 = 关闭(该测试 app 的 target=mock/model,无 fast 档)
        r2 = await client.put("/v1/admin/settings", json={"values": {
            "llm.draft_model": "",
        }})
        assert r2.status_code == 200
        assert api_app.state.config.model_draft == ""
        assert api_app.state.config.model_for_draft(
            "kb_init", "standard") == "mock/model"

    async def test_non_admin_forbidden(self, user_client):
        assert (await user_client.get("/v1/admin/settings")).status_code == 403
        assert (await user_client.put("/v1/admin/settings",
                                      json={"values": {"app.hitl": True}})).status_code == 403

    async def test_org_extensions_switch_flips_live_consumers(
            self, client, api_app, tmp_path, settings_store):
        """PUT 一个开关 → 已挂载的消费方(idle 的 SkillService,持同一把活
        开关的引用)当场改变行为,不重建、不重启 ——「生效」的端到端半边。

        断言分三层:响应值 / 活 config 对象 / **真实消费方行为**(org 注入
        停止,code skills 照常)。
        """
        from trove.services.skills.service import SkillService

        await _with_store(api_app, tmp_path, settings_store)
        svc = SkillService(
            root=tmp_path / "proj" / ".trove" / "skills",
            config=api_app.state.config,      # 与运行时同一把开关(活引用)
        )
        svc.create({"name": "org-rule", "description": "d", "tier": "required",
                    "triggers": {"node": "query_sketch"}, "body": "ORG-MARKER"})
        svc.confirm("org-rule")
        assert "ORG-MARKER" in svc.render_skills("query_sketch", lang="en")

        r = await client.put("/v1/admin/settings", json={"values": {
            "extensions.org_extensions_enabled": False,
        }})
        assert r.status_code == 200
        assert r.json()["values"]["extensions.org_extensions_enabled"] is False
        assert api_app.state.config.extensions.org_extensions_enabled is False
        rendered = svc.render_skills("query_sketch", lang="en")
        assert "ORG-MARKER" not in rendered                 # org 消费面停
        assert "Traceability self-check" in rendered        # code skills 不动

        r = await client.put("/v1/admin/settings", json={"values": {
            "extensions.org_extensions_enabled": True,
        }})
        assert r.status_code == 200
        assert "ORG-MARKER" in svc.render_skills("query_sketch", lang="en")