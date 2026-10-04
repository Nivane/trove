"""Config loading tests."""


import pytest

from trove.core.config import (
    ConfigLoader,
    AgentConfig,
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

    def test_context_budget_tokens(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text(
            "agent:\n"
            "  context_budget_tokens:\n"
            "    simple: 1500\n"
            "    standard: 2500\n"
            "    complex: 8000\n"
            "  schema_budget_tokens:\n"
            "    complex: 6000\n"
        )

        config = ConfigLoader.load_agent_config(str(config_file))
        assert config.context_budget_tokens == {
            "simple": 1500, "standard": 2500, "complex": 8000,
        }
        assert config.schema_budget_tokens == {"complex": 6000}

    def test_context_budget_tokens_default_empty(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text("agent:\n  target: openai/gpt-4o\n")

        config = ConfigLoader.load_agent_config(str(config_file))
        assert config.context_budget_tokens == {}
        assert config.schema_budget_tokens == {}

    def test_budget_loaded_from_yaml(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text(
            "agent:\n"
            "  budget:\n"
            "    timeout_ms: 45000\n"
            "    soft_scan_rows: 1000000\n"
            "    hard_scan_rows: 9000000\n"
            "    assume_max_scan_bytes: 1073741824\n"
            "    on_unestimable: reject\n"
        )

        config = ConfigLoader.load_agent_config(str(config_file))
        b = config.budget
        assert b.timeout_ms == 45_000
        assert b.soft_scan_rows == 1_000_000
        assert b.hard_scan_rows == 9_000_000
        assert b.assume_max_scan_bytes == 1024**3
        assert b.on_unestimable == "reject"

    def test_budget_defaults_match_the_market_profile(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text("agent:\n  target: openai/gpt-4o\n")

        b = ConfigLoader.load_agent_config(str(config_file)).budget
        assert b.timeout_ms == 30_000
        assert b.soft_scan_rows == 50_000_000
        assert b.hard_scan_rows == 1_000_000_000
        assert b.assume_max_scan_bytes == 20 * 1024**3
        # 方向默认必须**不是** reject:§8.3 C —— 过严的护栏会被绕过
        # (用户去直连库),那连观测都没有了
        assert b.on_unestimable == "degrade"

    def test_legacy_explain_caps_feed_the_budget_thresholds(self, tmp_path):
        """兼容读取:旧键 ``explain_max_rows`` / ``explain_hard_max_rows``
        已由 ``agent.budget.*`` 取代(留痕不删),数值原封不动搬过来。

        没有这一条,存量部署升级后阈值会**静默回到默认值** —— 一个改过上限的
        环境会突然按 50M/1B 判定,而没人察觉。
        """
        config_file = tmp_path / "agent.yml"
        config_file.write_text(
            "agent:\n"
            "  explain_max_rows: 7000000\n"
            "  explain_hard_max_rows: 70000000\n"
        )

        b = ConfigLoader.load_agent_config(str(config_file)).budget
        assert b.soft_scan_rows == 7_000_000
        assert b.hard_scan_rows == 70_000_000

    def test_budget_wins_over_legacy_keys(self, tmp_path):
        """两个键都在时以 budget 为准 —— 阈值只能有一个来源。"""
        config_file = tmp_path / "agent.yml"
        config_file.write_text(
            "agent:\n"
            "  explain_max_rows: 7000000\n"
            "  budget:\n"
            "    soft_scan_rows: 8000000\n"
        )

        b = ConfigLoader.load_agent_config(str(config_file)).budget
        assert b.soft_scan_rows == 8_000_000
        assert b.hard_scan_rows == 1_000_000_000  # 未覆盖的那档仍回落到旧键/默认

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

    def test_attribution_defaults(self):
        cfg = AgentConfig()
        assert cfg.attribution.probe_dimensions is True
        assert cfg.attribution.ratio_decomposition is True
        assert cfg.attribution.max_hops == 2

    def test_attribution_loaded_from_yaml(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text(
            "agent:\n"
            "  attribution:\n"
            "    max_hops: 3\n"
            "    max_dimensions: 4\n"
            "    probe_dimensions: false\n"
            "    ratio_decomposition: false\n"
        )
        config = ConfigLoader.load_agent_config(str(config_file))
        assert config.attribution.max_hops == 3
        assert config.attribution.max_dimensions == 4
        assert config.attribution.probe_dimensions is False
        assert config.attribution.ratio_decomposition is False

    def test_analysis_series_defaults_off(self):
        """块序列默认关(series_grain 空)—— 老路径逐字节不变的前提。"""
        cfg = AgentConfig()
        assert cfg.analysis.series_grain == ""
        assert cfg.analysis.block_lookback == 12

    def test_analysis_series_loaded_from_yaml(self, tmp_path):
        config_file = tmp_path / "agent.yml"
        config_file.write_text(
            "agent:\n"
            "  target: mock/model\n"
            "  analysis:\n"
            "    series_grain: Month\n"          # 大小写/空白都归一
            "    block_lookback: 24\n"
        )
        cfg = ConfigLoader.load_agent_config(str(config_file))
        assert cfg.analysis.series_grain == "month"
        assert cfg.analysis.block_lookback == 24

    def test_analysis_unknown_grain_folds_to_off(self, tmp_path):
        """错拼的粒度不假装序列开着(引擎侧只会得到 no_blocks 降级,
        不如在这里归空)—— 一个假开着的配置比一条报错更难查。"""
        config_file = tmp_path / "agent.yml"
        config_file.write_text(
            "agent:\n  target: mock/model\n"
            "  analysis:\n    series_grain: quarterly\n",
        )
        assert ConfigLoader.load_agent_config(
            str(config_file)).analysis.series_grain == ""

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


def test_retention_config_defaults():
    """缺省时 RetentionConfig 使用文档默认值。"""
    from trove.core.config import RetentionConfig

    cfg = RetentionConfig()
    assert cfg.max_sessions_per_user == 100
    assert cfg.active_grace_min == 10
    assert cfg.max_checkpoints_per_thread == 50
    assert cfg.sweep_interval_hours == 24


def test_retention_config_from_yaml(tmp_path):
    """agent.yml 的 retention 段被正确解析(含 0=关闭 语义)。"""
    from trove.core.config import ConfigLoader

    yml = tmp_path / "agent.yml"
    yml.write_text(
        "agent:\n"
        "  target: mock/model\n"
        "  retention:\n"
        "    max_sessions_per_user: 0\n"
        "    active_grace_min: 5\n"
        "    sweep_interval_hours: 6\n"
    )
    cfg = ConfigLoader.load_agent_config(str(yml))
    assert cfg.retention.max_sessions_per_user == 0
    assert cfg.retention.active_grace_min == 5
    assert cfg.retention.sweep_interval_hours == 6
    assert cfg.retention.max_checkpoints_per_thread == 50  # 未写 → 默认


class TestPerNodeModel:
    def test_model_for_node_pinned_wins(self):
        cfg = AgentConfig(
            target="openai/gpt-4o", model_fast="openai/gpt-4o-mini",
            node_models={"reflect": "deepseek/deepseek-reasoner"},
        )
        assert cfg.model_for_node("reflect", "simple") == "deepseek/deepseek-reasoner"
        assert cfg.model_for_node("reflect", "complex") == "deepseek/deepseek-reasoner"

    def test_model_for_node_falls_back_to_complexity(self):
        cfg = AgentConfig(
            target="openai/gpt-4o", model_fast="openai/gpt-4o-mini",
            node_models={"reflect": "x/y"},
        )
        # 未配节点 → 复杂度分档
        assert cfg.model_for_node("gen_sql", "simple") == "openai/gpt-4o-mini"
        assert cfg.model_for_node("gen_sql", "complex") == "openai/gpt-4o"
        # 配了节点但未用 → 回落复杂度
        assert cfg.model_for_node("insights", "standard") == "openai/gpt-4o-mini"

    def test_config_loader_parses_node_models(self, tmp_path):
        from trove.core.config import ConfigLoader

        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n  node_models:\n"
            "    query_sketch: openai/gpt-4o-mini\n    reflect: deepseek/reasoner\n",
            encoding="utf-8",
        )
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.node_models == {"query_sketch": "openai/gpt-4o-mini", "reflect": "deepseek/reasoner"}
        assert cfg.model_for_node("query_sketch", "complex") == "openai/gpt-4o-mini"


class TestGenSqlSoftRounds:
    def test_code_default_is_off(self):
        """代码默认关(0):库嵌入方与测试零行为变化,本仓走 conf 写 5。"""
        assert AgentConfig().gen_sql_soft_rounds == 0

    def test_config_loader_parses_soft_rounds(self, tmp_path):
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n  gen_sql_soft_rounds: 5\n",
            encoding="utf-8",
        )
        assert ConfigLoader.load_agent_config(str(conf)).gen_sql_soft_rounds == 5

        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n  gen_sql_soft_rounds: -3\n",
            encoding="utf-8",
        )
        assert ConfigLoader.load_agent_config(str(conf)).gen_sql_soft_rounds == 0


class TestHomeNormalization:
    """home 必须是展开后的绝对路径。

    Path("~/.trove") 是**相对**路径("~" 只是普通目录名),只有 expanduser()
    才会换成家目录。这个裸字符串曾被直接喂给 trace store,于是从仓库根跑的
    每一次 REPL/serve 都在 cwd 下建出 ./~/.trove/。归一化收在配置层这一处,
    所有下游消费者(含未来新增的)同时受益。
    """

    def test_default_home_is_expanded(self):
        from pathlib import Path

        cfg = AgentConfig()
        assert "~" not in cfg.home
        assert Path(cfg.home).is_absolute()

    def test_yaml_without_home_key_yields_expanded_home(self, tmp_path):
        from pathlib import Path

        conf = tmp_path / "agent.yml"
        conf.write_text("agent:\n  target: openai/gpt-4o\n", encoding="utf-8")
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert "~" not in cfg.home
        assert Path(cfg.home).is_absolute()


class TestMaskingAndAuthzConfig:
    """``masking:`` / ``authz:`` 两块 YAML 必须真的进 ``AgentConfig``(设计 §7.2)。

    ⚠️ 这一组钉的是**补 P3 的漏**:``AuthzConfig`` 的字段与默认值当时就加好了,
    加载器却从没构造过它 —— YAML 里写了 ``authz:`` 也恒取默认(warn/true),一处
    **静默失效的安全配置**:把 table_enforcement 改成 enforce 的人会以为闸门落
    下来了,实际上没有。所以断言的不是「解析得对不对」,是「配置面的开关连到了
    运行时」—— 这类洞只有把 YAML 喂进加载器才照得出来,``AuthzConfig()`` 直接
    构造的测试永远发现不了。
    """

    def test_masking_block_is_parsed(self, tmp_path):
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n"
            "masking:\n  enabled: false\n  hash_salt_ref: env:TROVE_MASK_SALT\n",
            encoding="utf-8",
        )
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.masking.enabled is False
        assert cfg.masking.hash_salt_ref == "env:TROVE_MASK_SALT"

    def test_masking_block_reads_nested_under_agent(self, tmp_path):
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n"
            "  masking:\n    enabled: false\n",
            encoding="utf-8",
        )
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.masking.enabled is False

    def test_authz_block_is_parsed(self, tmp_path):
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n"
            "authz:\n  table_enforcement: enforce\n  require_principal: false\n",
            encoding="utf-8",
        )
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.authz.table_enforcement == "enforce"
        assert cfg.authz.require_principal is False

    def test_authz_enum_is_normalized(self, tmp_path):
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n"
            "authz:\n  table_enforcement: '  ENFORCE  '\n",
            encoding="utf-8",
        )
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.authz.table_enforcement == "enforce"

    def test_absent_blocks_keep_the_protective_defaults(self, tmp_path):
        """缺席 = 闸门与脱敏都**开着**(默认值是「保护开着」,关它必须显式写)。

        table_enforcement 的默认是 **enforce**:2026-10 切换(650 条快径示例
        SQL 静态核验违规 0 条,403 面等于零)。回退到观察档要显式写 warn。
        """
        conf = tmp_path / "agent.yml"
        conf.write_text("agent:\n  target: openai/gpt-4o\n", encoding="utf-8")
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.masking.enabled is True
        assert cfg.masking.hash_salt_ref == ""
        assert cfg.authz.require_principal is True
        assert cfg.authz.table_enforcement == "enforce"

    def test_authz_defaults_hold_for_both_spellings(self, tmp_path):
        """「有 authz 段但没写档位」与「完全没有 authz 段」必须同档。

        三处默认值同源(dataclass / 这里的加载器缺省 / _build_authorizer 的
        getattr 兜底):只改一处,「写不写 authz 段」会静默改变闸门宽度,
        而配置面看起来什么都没发生 —— 这正是这段代码注释里写的教训。
        """
        for body in (
            "agent:\n  target: openai/gpt-4o\n",                      # 无 authz 段
            "agent:\n  target: openai/gpt-4o\nauthz: {}\n",           # 空段(顶层)
            "agent:\n  target: openai/gpt-4o\n  authz: {}\n",         # 空段(内嵌)
        ):
            conf = tmp_path / "agent.yml"
            conf.write_text(body, encoding="utf-8")
            cfg = ConfigLoader.load_agent_config(str(conf))
            assert cfg.authz.table_enforcement == "enforce", body
            assert cfg.authz.require_principal is True, body

    def test_authz_warn_rollback_is_explicit(self, tmp_path):
        """回退阀:warn 仍可显式选用(存量部署的回退路径)。"""
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n"
            "authz:\n  table_enforcement: warn\n",
            encoding="utf-8",
        )
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.authz.table_enforcement == "warn"


class TestConfidenceScoreFlag:
    def test_defaults_on(self, tmp_path):
        """默认开:零成本(纯函数),与 answer_source 一致 —— 那个没有开关。"""
        p = tmp_path / "agent.yml"
        p.write_text("agent:\n  target: mock/model\n")
        assert ConfigLoader.load_agent_config(str(p)).confidence_score is True

    def test_explicit_off(self, tmp_path):
        """急停开关:配置里显式 false 要读得到(它是唯一的急停手段)。"""
        p = tmp_path / "agent.yml"
        p.write_text("agent:\n  target: mock/model\n  confidence_score: false\n")
        assert ConfigLoader.load_agent_config(str(p)).confidence_score is False


class TestLyingConfigKeys:
    """conf/agent.yml 里写了、代码却不读的键 —— 每一条都是「改它没用」的谎言。

    这类键比死代码危险:死代码只是占地方,假开关在有人依赖它的那一刻收钱。
    """

    def test_decompose_llm_judge_from_yaml_is_honoured(self, tmp_path):
        """写 false 必须真的关掉 LLM 判断层(否则这条键就是摆设)。

        改前必红:字段声明在 AgentConfig 上、读取点在 session.py,唯独加载器
        的构造调用里漏了它 —— 于是 yaml 写什么都不生效,永远停在默认 True。
        """
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n  decompose_llm_judge: false\n",
            encoding="utf-8",
        )
        assert ConfigLoader.load_agent_config(str(conf)).decompose_llm_judge is False

    def test_decompose_llm_judge_defaults_to_on(self, tmp_path):
        conf = tmp_path / "agent.yml"
        conf.write_text("agent:\n  target: openai/gpt-4o\n", encoding="utf-8")
        assert ConfigLoader.load_agent_config(str(conf)).decompose_llm_judge is True

    def test_tracing_default_is_not_suppressed(self):
        """tracing.enabled 缺省 True = 不抑制(与「保护开着」同一条原则)。

        凭证才是真正的开关:没人会误配 LANGFUSE_*。这个键的职责是撤回,
        所以缺省必须是「不撤回」—— 否则少写一个 observability 段就等于
        静默停录,那是观测系统最坏的失败形态。
        """
        from trove.core.config import TracingConfig

        assert TracingConfig().enabled is True

    def test_tracing_default_survives_the_yaml_loader(self, tmp_path):
        """缺省要走**加载器那条路**验证,不能只测 dataclass。

        改前必红:字段缺省是 True,加载器却写 ``.get("enabled", False)``
        —— 一份没有 observability 段的配置加载出来是 False。只测
        ``TracingConfig()`` 的话,这个不一致永远看不见:字段自己是对的,
        是"从 yaml 到字段"这一段在撒谎。三处缺省(字段 / 加载器 /
        ``observability._suppressed``)必须同向。
        """
        conf = tmp_path / "agent.yml"
        conf.write_text("agent:\n  target: openai/gpt-4o\n", encoding="utf-8")
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.tracing.enabled is True

        # 空 observability 段同理(与"少写整段"是同一类误配)。
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n  observability: {}\n",
            encoding="utf-8",
        )
        assert ConfigLoader.load_agent_config(str(conf)).tracing.enabled is True

    def test_tracing_disabled_in_yaml_reaches_the_config(self, tmp_path):
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n"
            "  observability:\n    tracing:\n      enabled: false\n",
            encoding="utf-8",
        )
        assert ConfigLoader.load_agent_config(str(conf)).tracing.enabled is False


class TestActionConfig:
    """``action:`` 块必须真的进 ``AgentConfig``(照 ``TestMaskingAndAuthzConfig``
    的教训:字段加好了、加载器没读,是一处**静默失效的安全配置** —— 一份
    ``enabled: true`` 的部署跑成"关着",外送永不发生而配置面看起来一切正常)。
    """

    def test_action_block_is_parsed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TROVE_OPS_SECRET", "hush")
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n"
            "action:\n"
            "  enabled: true\n"
            "  approval_ttl_hours: 24\n"
            "  max_payload_bytes: 4096\n"
            "  max_attempts: 5\n"
            "  channels:\n"
            "    ops-alerts:\n"
            "      url: https://hook.example/ops\n"
            "      secret: ${TROVE_OPS_SECRET}\n",
            encoding="utf-8",
        )
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.action.enabled is True
        assert cfg.action.approval_ttl_hours == 24
        assert cfg.action.max_payload_bytes == 4096
        assert cfg.action.max_attempts == 5
        assert cfg.action.channels["ops-alerts"].url == "https://hook.example/ops"
        # secret 的 ``${ENV_VAR}`` 在加载时解析 —— 密钥不进 YAML 本体,
        # 通道配置因此可以随代码评审而不携带任何凭证。
        assert cfg.action.channels["ops-alerts"].secret == "hush"

    def test_action_block_reads_nested_under_agent(self, tmp_path):
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n"
            "  action:\n    enabled: true\n    channels:\n"
            "      room:\n        url: https://hook.example/room\n",
            encoding="utf-8",
        )
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.action.enabled is True
        assert cfg.action.channels["room"].url == "https://hook.example/room"

    def test_absent_block_keeps_the_layer_off(self, tmp_path):
        """缺席 = 行动层**关着**:一条能往外发消息的链路,不能靠"配置不在"
        顺手打开 —— 开它必须显式写 ``enabled: true``。"""
        conf = tmp_path / "agent.yml"
        conf.write_text("agent:\n  target: openai/gpt-4o\n", encoding="utf-8")
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.action.enabled is False
        assert cfg.action.channels == {}
        assert cfg.action.approval_ttl_hours == 72
        assert cfg.action.max_attempts == 3

    def test_channel_kind_defaults_to_generic_and_is_read(self, tmp_path):
        """通道 kind:缺席 = ``generic``(载荷原样外送,与塑形器不存在时一致)。"""
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n"
            "action:\n  channels:\n"
            "    ops-alerts:\n      url: https://hook.example/ops\n"
            "    slack-room:\n      url: https://hooks.slack.example/x\n"
            "      kind: slack\n",
            encoding="utf-8",
        )
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.action.channels["ops-alerts"].kind == "generic"
        assert cfg.action.channels["slack-room"].kind == "slack"

    def test_guard_fields_default_off_and_are_read(self, tmp_path):
        """护栏字段必须真的进 config(加载器不读 = 一份永远不生效的配置)。"""
        conf = tmp_path / "agent.yml"
        conf.write_text("agent:\n  target: openai/gpt-4o\n", encoding="utf-8")
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.action.max_risk == "high"        # 顶格 = 不设限
        assert cfg.action.rate_limit == 0           # 不限速
        assert cfg.action.retry_backoff_base_s == 0  # 不自动重试
        assert cfg.action.retry_backoff_factor == 2.0
        assert cfg.action.retry_backoff_max_s == 3600
        assert cfg.action.outcome_after_days == 0    # 不测量(B7 默认关)

        conf2 = tmp_path / "agent2.yml"
        conf2.write_text(
            "agent:\n  target: openai/gpt-4o\n"
            "action:\n  max_risk: Medium\n  rate_limit: 3\n"
            "  retry_backoff_base_s: 30\n  retry_backoff_factor: 1.5\n"
            "  retry_backoff_max_s: 600\n  outcome_after_days: 14\n",
            encoding="utf-8",
        )
        cfg2 = ConfigLoader.load_agent_config(str(conf2))
        assert cfg2.action.max_risk == "medium"     # 归一化到小写
        assert cfg2.action.rate_limit == 3
        assert cfg2.action.retry_backoff_base_s == 30
        assert cfg2.action.retry_backoff_factor == 1.5
        assert cfg2.action.retry_backoff_max_s == 600
        assert cfg2.action.outcome_after_days == 14

    def test_negative_guards_are_clamped_to_off(self, tmp_path):
        """负数 = 关(不是"负速率"这类没意义的值);缺省档不该被手滑配置打破。"""
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n"
            "action:\n  rate_limit: -5\n  retry_backoff_base_s: -1\n",
            encoding="utf-8",
        )
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.action.rate_limit == 0
        assert cfg.action.retry_backoff_base_s == 0

    def test_empty_block_and_absent_block_are_same_tier(self, tmp_path):
        """「有 action 段但没写 enabled」与「完全没有 action 段」同档(False)。"""
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\naction: {}\n", encoding="utf-8")
        assert ConfigLoader.load_agent_config(str(conf)).action.enabled is False


class TestScanConfig:
    """``scan:`` 块必须真的进 ``AgentConfig``(同 ``TestActionConfig`` 的教训:
    字段加好、加载器没读 = 一处静默失效的配置开关)。两个假设开关是本层
    最要紧的两个默认:定时扫描的 ``hypotheses`` 默认**关**(成本面在后台),
    交互侧的 ``interactive_hypotheses`` 默认**开**(一次分析尾部的一两跳)。
    """

    def test_defaults(self):
        cfg = AgentConfig()
        assert cfg.scan.hypotheses is False
        assert cfg.scan.interactive_hypotheses is True
        assert cfg.scan.max_hypotheses == 3
        assert cfg.scan.max_queries == 12
        assert cfg.scan.top_k == 5
        assert cfg.scan.lookback == 12
        assert cfg.scan.k == 3.5

    def test_block_is_parsed_top_level(self, tmp_path):
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n"
            "scan:\n"
            "  hypotheses: true\n"
            "  interactive_hypotheses: false\n"
            "  max_hypotheses: 5\n"
            "  max_queries: 20\n"
            "  top_k: 8\n"
            "  lookback: 24\n"
            "  k: 2.5\n",
            encoding="utf-8",
        )
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.scan.hypotheses is True
        assert cfg.scan.interactive_hypotheses is False
        assert cfg.scan.max_hypotheses == 5
        assert cfg.scan.max_queries == 20
        assert cfg.scan.top_k == 8
        assert cfg.scan.lookback == 24
        assert cfg.scan.k == 2.5

    def test_block_reads_nested_under_agent(self, tmp_path):
        """顶层与 ``agent:`` 内嵌两种都认(与 decision/action 同写法)。"""
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n"
            "  scan:\n    top_k: 2\n",
            encoding="utf-8",
        )
        assert ConfigLoader.load_agent_config(str(conf)).scan.top_k == 2

    def test_bad_numeric_fields_fall_back_or_clamp(self, tmp_path):
        """坏值不许把扫描推向"零发现/无预算"档:非数 k 回默认,计数下限 1。"""
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\n"
            "scan:\n  k: not-a-number\n  top_k: 0\n  lookback: -3\n"
            "  max_queries: 0\n  max_hypotheses: 0\n",
            encoding="utf-8",
        )
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.scan.k == 3.5
        assert cfg.scan.top_k == 1
        assert cfg.scan.lookback == 1
        assert cfg.scan.max_queries == 1
        assert cfg.scan.max_hypotheses == 1

    def test_k_must_be_positive_and_finite(self, tmp_path):
        """``k <= 0`` 的带宽会把每一点都判成异常 —— 退回默认,不接。"""
        conf = tmp_path / "agent.yml"
        for bad in ("k: 0\n", "k: -2.5\n", "k: .inf\n"):
            conf.write_text(
                "agent:\n  target: openai/gpt-4o\nscan:\n  " + bad,
                encoding="utf-8",
            )
            assert ConfigLoader.load_agent_config(str(conf)).scan.k == 3.5

    def test_empty_block_and_absent_block_are_same_tier(self, tmp_path):
        conf = tmp_path / "agent.yml"
        conf.write_text(
            "agent:\n  target: openai/gpt-4o\nscan: {}\n", encoding="utf-8")
        cfg = ConfigLoader.load_agent_config(str(conf))
        assert cfg.scan.hypotheses is False
        assert cfg.scan.interactive_hypotheses is True
