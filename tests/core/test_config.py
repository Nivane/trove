"""Config loading tests."""

import os
from pathlib import Path

import pytest

from trove.core.config import (
    ConfigLoader,
    AgentConfig,
    ProjectConfig,
    PROJECT_CONFIG_WHITELIST,
)
from trove.core.errors import ConfigError


class TestEnvVarResolution:
    def test_single_var(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "secret-value")
        result = ConfigLoader.resolve_env_vars("${TEST_KEY}")
        assert result == "secret-value"

    def test_var_in_middle_of_string(self, monkeypatch):
        monkeypatch.setenv("HOST", "localhost")
        result = ConfigLoader.resolve_env_vars("postgres://${HOST}:5432/db")
        assert result == "postgres://localhost:5432/db"

    def test_missing_var_becomes_empty(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        result = ConfigLoader.resolve_env_vars("${NONEXISTENT_VAR}")
        assert result == ""

    def test_no_vars_unchanged(self):
        result = ConfigLoader.resolve_env_vars("plain text")
        assert result == "plain text"


class TestEnvVarResolutionInDict:
    def test_nested_dict(self, monkeypatch):
        monkeypatch.setenv("PG_PASSWORD", "pw123")
        data = {
            "agent": {
                "services": {
                    "datasources": [
                        {
                            "name": "pg",
                            "connection": {"password": "${PG_PASSWORD}"},
                        }
                    ]
                }
            }
        }
        resolved = ConfigLoader.resolve_env_vars_in_dict(data)
        assert resolved["agent"]["services"]["datasources"][0]["connection"]["password"] == "pw123"

    def test_list_of_dicts(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "key-abc")
        data = {"providers": [{"api_key": "${API_KEY}"}]}
        resolved = ConfigLoader.resolve_env_vars_in_dict(data)
        assert resolved["providers"][0]["api_key"] == "key-abc"


class TestConfigFileSearch:
    def test_find_explicit_path(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text("agent:\n  target: test-model\n")
        found = ConfigLoader.find_config_file(str(config_file))
        assert found is not None
        assert found.name == "agent.yml"

    def test_explicit_path_missing(self, tmp_path):
        missing = tmp_path / "nope.yml"
        assert ConfigLoader.find_config_file(str(missing)) is None

    def test_search_order_prefers_cwd(self, tmp_path, monkeypatch):
        # Create ./conf/agent.yml relative to cwd
        conf_dir = tmp_path / "conf"
        conf_dir.mkdir()
        cwd_conf = conf_dir / "agent.yml"
        cwd_conf.write_text("agent:\n  target: from-cwd\n")

        monkeypatch.chdir(tmp_path)
        found = ConfigLoader.find_config_file()
        assert found == cwd_conf


class TestLoadAgentConfig:
    def test_load_basic(self, tmp_path, monkeypatch):
        config_file = tmp_path / "agent.yml"
        config_file.write_text(
            "agent:\n"
            "  target: openai/gpt-4o\n"
            "  language: zh\n"
            "  home: /tmp/trove-home\n"
        )

        config = ConfigLoader.load_agent_config(str(config_file))
        assert config.target == "openai/gpt-4o"
        assert config.language == "zh"
        assert config.home == "/tmp/trove-home"

    def test_load_with_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_KEY", "sk-test")
        config_file = tmp_path / "agent.yml"
        config_file.write_text(
            "agent:\n"
            "  providers:\n"
            "    - name: openai\n"
            "      litellm_params:\n"
            "        api_key: ${OPENAI_KEY}\n"
        )

        config = ConfigLoader.load_agent_config(str(config_file))
        assert config.providers[0].litellm_params["api_key"] == "sk-test"

    def test_load_with_datasources(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text(
            "agent:\n"
            "  services:\n"
            "    datasources:\n"
            "      - name: prod\n"
            "        type: postgres\n"
            "        connection:\n"
            "          host: localhost\n"
        )

        config = ConfigLoader.load_agent_config(str(config_file))
        assert len(config.datasources) == 1
        assert config.datasources[0].name == "prod"
        assert config.datasources[0].type == "postgres"

    def test_load_with_semantic_layer_path(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text(
            "agent:\n"
            "  semantic_layer_path: .trove/semantic\n"
        )

        config = ConfigLoader.load_agent_config(str(config_file))
        assert config.semantic_layer_path == ".trove/semantic"

    def test_semantic_layer_path_defaults_empty(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text("agent:\n  target: openai/gpt-4o\n")

        config = ConfigLoader.load_agent_config(str(config_file))
        assert config.semantic_layer_path == ""

    def test_load_invalid_yaml_raises(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text("agent: [unclosed\n")

        with pytest.raises(ConfigError):
            ConfigLoader.load_agent_config(str(config_file))

    def test_load_missing_file_returns_empty(self):
        config = ConfigLoader.load_agent_config("/nonexistent/path.yml")
        assert config.target == ""
        assert config.providers == []


class TestLoadProjectConfig:
    def test_empty_when_no_file(self, tmp_path):
        config = ConfigLoader.load_project_config(tmp_path)
        assert config.target == ""
        assert config.default_datasource == ""

    def test_load_whitelisted_keys(self, tmp_path):
        trove_dir = tmp_path / ".trove"
        trove_dir.mkdir()
        (trove_dir / "config.yml").write_text(
            "target: claude-sonnet-5\n"
            "default_datasource: prod\n"
        )
        config = ConfigLoader.load_project_config(tmp_path)
        assert config.target == "claude-sonnet-5"
        assert config.default_datasource == "prod"

    def test_non_whitelisted_keys_filtered(self, tmp_path):
        trove_dir = tmp_path / ".trove"
        trove_dir.mkdir()
        (trove_dir / "config.yml").write_text(
            "target: model-1\n"
            "api_key: should-not-load\n"
            "secret_password: nope\n"
        )
        config = ConfigLoader.load_project_config(tmp_path)
        assert config.target == "model-1"
        # Non-whitelisted keys are silently dropped (not in ProjectConfig)


class TestWhitelist:
    def test_whitelist_contains_expected_keys(self):
        assert "target" in PROJECT_CONFIG_WHITELIST
        assert "default_datasource" in PROJECT_CONFIG_WHITELIST
        assert "project_name" in PROJECT_CONFIG_WHITELIST
        assert "scheduler" in PROJECT_CONFIG_WHITELIST

    def test_whitelist_excludes_credentials(self):
        assert "api_key" not in PROJECT_CONFIG_WHITELIST
        assert "password" not in PROJECT_CONFIG_WHITELIST


class TestAdaptiveLoadConfig:
    """fast_path / reflect_skip 配置键映射与缺省。"""

    def test_defaults_when_absent(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text("agent:\n  target: mock/model\n")
        config = ConfigLoader.load_agent_config(str(config_file))
        assert config.fast_path is True
        assert config.reflect_skip == "simple"

    def test_maps_explicit_values(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text(
            "agent:\n"
            "  fast_path: false\n"
            "  reflect_skip: all\n"
        )
        config = ConfigLoader.load_agent_config(str(config_file))
        assert config.fast_path is False
        assert config.reflect_skip == "all"


class TestModelTiering:
    """model_fast 字段缺省 / YAML 加载 / model_for 分档。"""

    def test_model_fast_default_empty(self):
        assert AgentConfig().model_fast == ""

    def test_model_fast_loaded_from_yaml(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text(
            "agent:\n"
            "  target: deepseek/deepseek-reasoner\n"
            "  model_fast: deepseek/deepseek-chat\n"
        )
        config = ConfigLoader.load_agent_config(str(config_file))
        assert config.target == "deepseek/deepseek-reasoner"
        assert config.model_fast == "deepseek/deepseek-chat"

    def test_result_cache_default_off(self):
        assert AgentConfig().result_cache is False

    def test_result_cache_loaded_from_yaml(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text("agent:\n  result_cache: true\n")
        config = ConfigLoader.load_agent_config(str(config_file))
        assert config.result_cache is True

    @pytest.mark.parametrize("model_fast,complexity,expected", [
        ("", "simple", "mock/target"),            # 未配置 fast → 不分档
        ("", "complex", "mock/target"),
        ("mock/fast", "simple", "mock/fast"),     # simple/standard → fast
        ("mock/fast", "standard", "mock/fast"),
        ("mock/fast", "complex", "mock/target"),  # complex 及未知 → target
        ("mock/fast", "anything", "mock/target"),
    ])
    def test_model_for_tiering(self, model_fast, complexity, expected):
        cfg = AgentConfig(target="mock/target", model_fast=model_fast)
        assert cfg.model_for(complexity) == expected

    def test_model_for_falls_back_to_gpt4o(self):
        assert AgentConfig().model_for("simple") == "openai/gpt-4o"
