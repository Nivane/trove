"""SettingsStore + settings-service policy tests (DB-backed runtime config)."""

from __future__ import annotations


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

    def test_validate_path_kind(self):
        """path 类目录配置:非空通过,空串=关闭(区别于 str 的必填)。"""
        coerced, errors = validate_values({"app.semantic_layer_path": ".trove/semantic"})
        assert errors == []
        assert coerced["app.semantic_layer_path"] == ".trove/semantic"
        # 清空 = 关闭语义层,允许
        coerced, errors = validate_values({"app.semantic_layer_path": ""})
        assert errors == []
        assert coerced["app.semantic_layer_path"] == ""

    def test_apply_overrides_semantic_layer_path(self):
        cfg = AgentConfig(target="openai/gpt-4o")
        apply_overrides(cfg, {"app.semantic_layer_path": ".trove/semantic"})
        assert cfg.semantic_layer_path == ".trove/semantic"
        apply_overrides(cfg, {"app.semantic_layer_path": ""})
        assert cfg.semantic_layer_path == ""

    def test_effective_values_semantic_layer_path(self):
        cfg = AgentConfig(semantic_layer_path=".trove/semantic")
        values = effective_values(cfg)
        assert values["app.semantic_layer_path"] == ".trove/semantic"

    def test_egress_strlist_accepts_list_and_comma_string(self):
        """白名单从管理端来的两种形状都要收:JSON 数组 / 逗号分隔字符串。"""
        coerced, errors = validate_values({
            "egress.allow_providers": ["OpenAI", " anthropic/claude-3 "],
        })
        assert errors == []
        assert coerced["egress.allow_providers"] == ["openai", "anthropic/claude-3"]

        coerced, errors = validate_values({
            "egress.allow_providers": " openai, ,DeepSeek ",
        })
        assert errors == []
        assert coerced["egress.allow_providers"] == ["openai", "deepseek"]

    def test_egress_strlist_dedupes_keeping_order(self):
        """白名单语义是集合:重复条目折掉,输出稳定便于回显比对。"""
        coerced, errors = validate_values({
            "egress.allow_providers": "openai, deepseek, OPENAI",
        })
        assert errors == []
        assert coerced["egress.allow_providers"] == ["openai", "deepseek"]

    def test_egress_strlist_rejects_non_list(self):
        _, errors = validate_values({"egress.allow_providers": {"openai": True}})
        assert errors and "invalid value" in errors[0]

    def test_egress_apply_overrides_mutates_live_config(self):
        """热更新:管理端保存后网关的 ``lambda: config.egress`` 现读到新值。"""
        cfg = AgentConfig()
        apply_overrides(cfg, {
            "egress.enabled": False,
            "egress.redact_tool_results": False,
            "egress.allow_providers": ["openai"],
        })
        assert cfg.egress.enabled is False
        assert cfg.egress.redact_tool_results is False
        assert cfg.egress.allow_providers == ["openai"]

    def test_egress_effective_values_echoes_allowlist(self):
        cfg = AgentConfig()
        cfg.egress.allow_providers = ["openai", "anthropic/claude-3"]
        values = effective_values(cfg)
        assert values["egress.allow_providers"] == ["openai", "anthropic/claude-3"]
        assert values["egress.enabled"] is True
        assert values["egress.redact_tool_results"] is True

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


class TestCliModelPrecedence:
    """`--model` 与 settings.db 撞同一个 config.target 时的胜者。

    README 承诺「CLI --model > conf/agent.yml」,而管理台的 llm.default_model 是
    第三条覆盖层。它赢过 yaml 是对的,赢过命令行这一次性的显式意图是错的。

    这里测的是 apply_runtime_overrides —— create_app_components 调的那一个函数,
    两层覆盖的**顺序**就写在它里面。所以抽掉里面的 CLI 重放,下面第一个用例必红。
    (残留缺口:若有人绕过这个函数直接调 apply_overrides,测试看不到。)
    """

    def test_cli_model_wins_over_db_override(self):
        from types import SimpleNamespace

        from trove.main import apply_runtime_overrides

        config = AgentConfig(target="openai/gpt-4o")  # conf/agent.yml
        apply_runtime_overrides(
            config, SimpleNamespace(model="anthropic/claude"),
            {"llm.default_model": "deepseek/deepseek-chat"},
        )
        assert config.target == "anthropic/claude"  # CLI > DB > yaml

    def test_db_override_still_wins_over_yaml(self):
        """不给 --model 时,管理台的值照旧压过 agent.yml。"""
        from types import SimpleNamespace

        from trove.main import apply_runtime_overrides

        config = AgentConfig(target="openai/gpt-4o")
        apply_runtime_overrides(
            config, SimpleNamespace(model=None),
            {"llm.default_model": "deepseek/deepseek-chat"},
        )
        assert config.target == "deepseek/deepseek-chat"

    def test_no_overrides_keeps_yaml_value(self):
        from types import SimpleNamespace

        from trove.main import apply_runtime_overrides

        config = AgentConfig(target="openai/gpt-4o")
        apply_runtime_overrides(config, SimpleNamespace(model=None), None)
        assert config.target == "openai/gpt-4o"


class TestRunRecordSettings:
    """请求记录的两个运行时开关:默认开 / 30 天,0 = 永久。"""

    def test_defaults(self):
        config = AgentConfig()
        assert config.tracing.run_records is True
        assert config.retention.run_record_retention_days == 30

    def test_validate_coerces(self):
        coerced, errors = validate_values({
            "observability.run_records": "false",
            "retention.run_record_retention_days": "0",
        })
        assert errors == []
        assert coerced["observability.run_records"] is False
        assert coerced["retention.run_record_retention_days"] == 0

    def test_negative_retention_is_rejected(self):
        _, errors = validate_values({"retention.run_record_retention_days": "-1"})
        assert errors == ["retention.run_record_retention_days must be >= 0"]

    def test_apply_and_read_back(self):
        config = AgentConfig()
        apply_overrides(config, {
            "observability.run_records": False,
            "retention.run_record_retention_days": 0,
        })
        assert config.tracing.run_records is False
        assert config.retention.run_record_retention_days == 0
        values = effective_values(config)
        assert values["observability.run_records"] is False
        assert values["retention.run_record_retention_days"] == 0

    def test_recorder_switch_reads_live_config(self):
        """管理端改值 → 下一问生效:recorder 每次 capture 现读 callable。"""
        from trove.tracing.run_record import RunRecorder

        config = AgentConfig()
        rec = RunRecorder(object(), enabled=lambda: config.tracing.run_records)
        assert rec.enabled() is True
        apply_overrides(config, {"observability.run_records": False})
        assert rec.enabled() is False
