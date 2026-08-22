"""SettingsStore + settings-service policy tests (DB-backed runtime config)."""

from __future__ import annotations

import pytest

from trove.services.admin_settings.service import (
    MASK,
    apply_overrides,
    effective_values,
    mask_providers,
    validate_values,
)
from trove.services.admin_settings.store import SettingsStore
from trove.core.config import AgentConfig


class TestSettingsStore:
    async def test_put_get_all_roundtrip(self, tmp_path):
        store = SettingsStore(tmp_path / "settings.db")
        await store.put_many({"app.hitl": True, "llm.default_model": "openai/gpt-4o",
                              "retention.max_sessions_per_user": 50})
        all_ = await store.get_all()
        assert all_["app.hitl"] is True
        assert all_["llm.default_model"] == "openai/gpt-4o"
        assert all_["retention.max_sessions_per_user"] == 50

    async def test_put_updates_in_place(self, tmp_path):
        store = SettingsStore(tmp_path / "settings.db")
        await store.put_many({"app.hitl": True})
        await store.put_many({"app.hitl": False, "app.insights": True})
        all_ = await store.get_all()
        assert all_["app.hitl"] is False
        assert all_["app.insights"] is True

    async def test_delete_and_prefix(self, tmp_path):
        store = SettingsStore(tmp_path / "settings.db")
        await store.put_many({"llm.default_model": "a", "llm.fast_model": "b",
                              "app.language": "zh"})
        await store.delete("app.language")
        await store.delete_prefix("llm.")
        all_ = await store.get_all()
        assert all_ == {}


class TestSettingsService:
    def test_apply_overrides_mutates_config(self):
        config = AgentConfig()
        overrides = {"app.hitl": True, "app.language": "zh",
                     "app.reflect_skip": "off",
                     "retention.max_sessions_per_user": 7,
                     "retention.sweep_interval_hours": 0}
        apply_overrides(config, overrides)
        assert config.hitl is True
        assert config.language == "zh"
        assert config.reflect_skip == "off"
        assert config.retention.max_sessions_per_user == 7
        assert config.retention.sweep_interval_hours == 0

    def test_apply_overrides_providers(self):
        config = AgentConfig()
        providers = [{"name": "openai", "litellm_params": {"api_key": "sk-abc",
                                                            "api_base": "https://x"}}]
        apply_overrides(config, {"llm.providers": providers})
        assert [p.name for p in config.providers] == ["openai"]
        assert config.providers[0].litellm_params["api_key"] == "sk-abc"

    def test_effective_values_flat_schema(self):
        config = AgentConfig(target="m", language="zh", hitl=True)
        values = effective_values(config)
        assert values["llm.default_model"] == "m"
        assert values["app.language"] == "zh"
        assert values["app.hitl"] is True
        assert values["retention.max_sessions_per_user"] == 100

    def test_validate_bool_enum_int(self):
        coerced, errors = validate_values({
            "app.hitl": "true",
            "app.language": "en",
            "app.reflect_skip": "standard",
            "retention.max_sessions_per_user": "20",
        })
        assert errors == []
        assert coerced["app.hitl"] is True
        assert coerced["app.language"] == "en"
        assert coerced["retention.max_sessions_per_user"] == 20

    def test_validate_rejects_bad_values(self):
        _, errors = validate_values({
            "app.language": "fr",
            "app.reflect_skip": "sometimes",
            "unknown.key": 1,
            "retention.max_sessions_per_user": -3,
        })
        joined = "; ".join(errors)
        assert "language" in joined
        assert "reflect_skip" in joined
        assert "unknown setting" in joined
        assert "sessions" in joined

    def test_validate_result_limits_range(self):
        """结果限制走 range 校验:越界 400,合法值直接透传并 apply。"""
        coerced, errors = validate_values({
            "app.result_display_rows": 50,
            "app.result_max_rows": 1000,
        })
        assert errors == []
        assert coerced["app.result_display_rows"] == 50
        assert coerced["app.result_max_rows"] == 1000

        _, errors = validate_values({
            "app.result_display_rows": 0,
            "app.result_max_rows": 2 ** 31,
        })
        assert errors and all("between" in e for e in errors)

    def test_apply_overrides_result_limits(self):
        cfg = AgentConfig(target="openai/gpt-4o")
        apply_overrides(cfg, {"app.result_display_rows": 80, "app.result_max_rows": 2000})
        assert cfg.result_display_rows == 80
        assert cfg.result_max_rows == 2000

    def test_provider_mask_roundtrip(self):
        config = AgentConfig()
        config.providers = [__import__("trove.core.config", fromlist=["ProviderConfig"])
                            .ProviderConfig(name="openai",
                                            litellm_params={"api_key": "sk-secret",
                                                            "api_base": "https://api.openai.com"})]
        masked = mask_providers(config.providers)
        assert masked[0]["has_api_key"] is True
        assert masked[0]["litellm_params"]["api_key"] == MASK

        # clients send the mask marker back; it resolves against the *stored*
        # (unmasked) value held on the server
        stored = [{"name": "openai", "litellm_params": {"api_key": "sk-secret",
                                                        "api_base": "https://api.openai.com"}}]
        coerced, errors = validate_values(
            {"llm.providers": [{"name": "openai",
                                "litellm_params": {"api_key": MASK,
                                                   "api_base": "https://new.base"}}]},
            stored,
        )
        assert errors == []
        params = coerced["llm.providers"][0]["litellm_params"]
        assert params["api_key"] == "sk-secret"
        assert params["api_base"] == "https://new.base"

    def test_validate_providers_without_mask(self):
        coerced, errors = validate_values(
            {"llm.providers": [{"name": "openai", "litellm_params": {"api_key": "sk-new"}}]},
            [],
        )
        assert errors == []
        assert coerced["llm.providers"][0]["litellm_params"]["api_key"] == "sk-new"