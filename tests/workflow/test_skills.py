"""Skill 模板:manifest 触发匹配 + 节点 system prompt 注入。

Skills are node-triggered methodology blocks (trove/prompts/skills/):
deterministic matching via manifest.yml, rendered by the prompt loader,
appended to the node's system prompt.
"""

from __future__ import annotations

import pytest

from trove.prompts.skills import matched_skills, render_skills
from trove.workflow.state import WorkflowState


@pytest.fixture(autouse=True)
def _fresh_skills_cache():
    """每例重置 manifest 缓存,保证测试间独立。"""
    from trove.prompts import skills

    skills._cache = None
    yield
    skills._cache = None


def make_state(**kwargs) -> WorkflowState:
    defaults = {"session_id": "s1", "question": "Average grade by county"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


def test_manifest_matches_planner_only():
    """plan_query 只由 planner 节点触发;其它节点不匹配。"""
    assert matched_skills("planner") == ["plan_query"]
    assert matched_skills("gen_sql") == []
    assert matched_skills("schema_linking") == []


def test_trigger_extra_ctx_does_not_break_match():
    """plan_query 只有 node 触发条件:额外的 ctx 特征不影响匹配。"""
    assert matched_skills("planner", error_class="column_mismatch") == ["plan_query"]


def test_render_skills_bilingual():
    en = render_skills("planner", lang="en")
    zh = render_skills("planner", lang="zh")
    assert "Traceability" in en
    assert "decomposition" in en.lower()
    assert "可追溯" in zh
    assert "分解" in zh


def test_render_skills_no_match_is_empty():
    assert render_skills("gen_sql", lang="en") == ""
    assert render_skills("answer", lang="zh") == ""


async def test_planner_system_prompt_includes_skill():
    """planner 的 system prompt 携带 plan_query skill 块,语言跟随 state.lang。"""
    from trove.core.config import AgentConfig
    from trove.workflow.nodes.planner import make_planner

    captured = {}

    class LLM:
        async def chat(self, model, messages, **kwargs):
            captured.update(messages=messages)
            return "plan"

    node = make_planner(LLM(), AgentConfig(target="m"), agentic=False)
    await node(make_state(question="平均成绩是多少", lang="zh"))
    system = captured["messages"][0]["content"]
    assert "规划" in system
    assert "可追溯" in system

    await node(make_state(question="average grade", lang="en"))
    system = captured["messages"][0]["content"]
    assert "query planner" in system
    assert "Traceability" in system
