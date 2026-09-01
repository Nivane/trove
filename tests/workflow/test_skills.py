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


def test_manifest_matches_by_node():
    """每个 skill 只由声明的节点触发;其它节点不匹配。"""
    assert matched_skills("query_sketch") == ["plan_query"]
    assert matched_skills("analyze_error") == ["diagnose_failure"]
    assert matched_skills("schema_linking") == ["align_schema"]
    assert matched_skills("gen_sql") == []
    assert matched_skills("answer") == []


def test_trigger_extra_ctx_does_not_break_match():
    """plan_query 只有 node 触发条件:额外的 ctx 特征不影响匹配。"""
    assert matched_skills("query_sketch", error_class="column_mismatch") == ["plan_query"]


def test_render_skills_bilingual():
    en = render_skills("query_sketch", lang="en")
    zh = render_skills("query_sketch", lang="zh")
    assert "Traceability" in en
    assert "decomposition" in en.lower()
    assert "可追溯" in zh
    assert "分解" in zh

    en = render_skills("analyze_error", lang="en")
    zh = render_skills("analyze_error", lang="zh")
    assert "Regression" in en
    assert "Rollback" in en
    assert "回归检查" in zh
    assert "回退纪律" in zh

    en = render_skills("schema_linking", lang="en")
    zh = render_skills("schema_linking", lang="zh")
    assert "necessity" in en.lower()
    assert "populated" in en.lower()
    assert "必要性" in zh
    assert "有数据" in zh


def test_render_skills_no_match_is_empty():
    assert render_skills("gen_sql", lang="en") == ""
    assert render_skills("answer", lang="zh") == ""


async def test_query_sketch_system_prompt_includes_skill():
    """query_sketch 的 system prompt 携带 plan_query skill 块,语言跟随 state.lang。"""
    from trove.core.config import AgentConfig
    from trove.workflow.nodes.query_sketch import make_query_sketch

    captured = {}

    class LLM:
        async def chat(self, model, messages, **kwargs):
            captured.update(messages=messages)
            return "plan"

    node = make_query_sketch(LLM(), AgentConfig(target="m"), agentic=False)
    await node(make_state(question="平均成绩是多少", lang="zh"))
    system = captured["messages"][0]["content"]
    assert "规划" in system
    assert "可追溯" in system

    await node(make_state(question="average grade", lang="en"))
    system = captured["messages"][0]["content"]
    assert "query query_sketch" in system
    assert "Traceability" in system


async def test_analyze_error_system_prompt_includes_skill():
    """analyze_error 的 system prompt 携带 diagnose_failure skill 块。"""
    from trove.core.config import AgentConfig
    from trove.workflow.nodes.analyze_error import make_analyze_error

    captured = {}

    class LLM:
        async def chat(self, model, messages, **kwargs):
            captured.update(messages=messages)
            return "类型: Filter\n判断: 条件过严\n修正: 放宽\nTARGET: query_sketch"

    node = make_analyze_error(LLM(), AgentConfig(target="m"))
    await node(make_state(
        sql="SELECT * FROM loan",
        error_feedback="no rows",
        schema_context="Table: loan",
        lang="zh",
    ))
    system = captured["messages"][0]["content"]
    assert "诊断流程" in system
    assert "回归" in system

    await node(make_state(
        sql="SELECT * FROM loan",
        error_feedback="no rows",
        schema_context="Table: loan",
        lang="en",
    ))
    system = captured["messages"][0]["content"]
    assert "Diagnosis procedure" in system
    assert "Regression" in system
