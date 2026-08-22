"""/v1/admin/settings — DB-backed runtime config (admin-only)."""

from __future__ import annotations

import pytest

from trove.services.admin_settings.service import MASK
from trove.services.admin_settings.store import SettingsStore


async def _with_store(api_app, tmp_path) -> SettingsStore:
    store = SettingsStore(tmp_path / "settings.db")
    api_app.state.settings = store
    return store


class TestSettingsApi:
    async def test_get_returns_effective_values(self, client, api_app, tmp_path):
        await _with_store(api_app, tmp_path)
        r = await client.get("/v1/admin/settings")
        assert r.status_code == 200
        body = r.json()
        values = body["values"]
        assert values["llm.default_model"] == "mock/model"
        assert values["app.language"] == "zh"
        assert values["app.hitl"] is False
        assert isinstance(values["llm.providers"], list)
        assert body["mask"] == MASK

    async def test_put_applies_and_persists(self, client, api_app, tmp_path):
        store = await _with_store(api_app, tmp_path)
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

    async def test_put_invalid_400_with_detail(self, client, api_app, tmp_path):
        await _with_store(api_app, tmp_path)
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

    async def test_put_result_limits_applied(self, client, api_app, tmp_path):
        """结果限制经 settings API 落库 + 热更新镜像到 pipeline 注册表。"""
        from trove.services.limits import get_result_limits

        store = await _with_store(api_app, tmp_path)
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

    async def test_put_provider_api_key_masking(self, client, api_app, tmp_path):
        store = await _with_store(api_app, tmp_path)
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

    async def test_non_admin_forbidden(self, user_client):
        assert (await user_client.get("/v1/admin/settings")).status_code == 403
        assert (await user_client.put("/v1/admin/settings",
                                      json={"values": {"app.hitl": True}})).status_code == 403