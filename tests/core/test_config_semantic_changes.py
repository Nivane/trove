"""ChangesConfig 的加载器接线：顶层与 agent: 内嵌两种写法都认，缺省与字段默认逐字一致。"""
from __future__ import annotations

from trove.core.config import AgentConfig, ChangesConfig, ConfigLoader


def _load(tmp_path, body: str) -> AgentConfig:
    path = tmp_path / "agent.yml"
    path.write_text(body, encoding="utf-8")
    # 加载器在 ConfigLoader 上（`ConfigLoader.load_agent_config(config_path)`）——
    # 这是仓内既有测试的统一写法（tests/core/test_config.py）。
    return ConfigLoader.load_agent_config(str(path))


def test_defaults_match_field_defaults(tmp_path):
    conf = _load(tmp_path, "target: mock/model\n")
    assert conf.semantic_changes == ChangesConfig()
    assert conf.semantic_changes.retain_staging_days == 30
    assert conf.semantic_changes.sandbox_by_origin == ["draft_confirm", "auto_apply"]


def test_top_level_spelling(tmp_path):
    conf = _load(tmp_path, (
        "target: mock/model\n"
        "semantic_changes:\n"
        "  retain_staging_days: 7\n"
        "  sandbox_by_origin: [manual]\n"
    ))
    assert conf.semantic_changes.retain_staging_days == 7
    assert conf.semantic_changes.sandbox_by_origin == ["manual"]


def test_nested_agent_spelling(tmp_path):
    conf = _load(tmp_path, (
        "agent:\n  semantic_changes:\n    retain_staging_days: 3\n"
    ))
    assert conf.semantic_changes.retain_staging_days == 3
