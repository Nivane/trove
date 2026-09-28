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


# ── Org skills (SkillService assets, admin-confirmed) ────


def _org_skills(tmp_path) -> "object":
    from trove.services.skills.service import SkillService

    return SkillService(root=tmp_path / ".trove" / "skills")


async def test_query_sketch_includes_confirmed_org_required_skill(tmp_path):
    """required 档已确认 org skill 全量注入 query_sketch system prompt。"""
    from trove.core.config import AgentConfig
    from trove.workflow.nodes.query_sketch import make_query_sketch

    svc = _org_skills(tmp_path)
    svc.create({
        "name": "org-plan", "description": "组织计划口径",
        "triggers": {"node": "query_sketch"}, "tier": "required",
        "body": "## 组织计划口径\n先固定分组再取极值",
    })
    svc.confirm("org-plan")

    captured = {}

    class LLM:
        async def chat(self, model, messages, **kwargs):
            captured.update(messages=messages)
            return "plan"

    node = make_query_sketch(LLM(), AgentConfig(target="m"), agentic=False, skills=svc)
    await node(make_state(question="平均成绩是多少", lang="zh"))
    assert "组织计划口径" in captured["messages"][0]["content"]


async def test_query_sketch_skips_pending_and_available_org_skills(tmp_path):
    """pending 草稿 / available 档不注入单发节点 system prompt。"""
    from trove.core.config import AgentConfig
    from trove.workflow.nodes.query_sketch import make_query_sketch

    svc = _org_skills(tmp_path)
    svc.create({
        "name": "opt-skill", "description": "可选口径",
        "triggers": {"node": "query_sketch"}, "tier": "available",
        "body": "## 可选口径\n按需才用",
    })
    svc.confirm("opt-skill")
    svc.create({
        "name": "draft-skill", "description": "草稿",
        "triggers": {"node": "query_sketch"}, "tier": "required",
        "body": "## 草稿口径\n未确认",
    })

    captured = {}

    class LLM:
        async def chat(self, model, messages, **kwargs):
            captured.update(messages=messages)
            return "plan"

    node = make_query_sketch(LLM(), AgentConfig(target="m"), agentic=False, skills=svc)
    await node(make_state(question="平均成绩", lang="zh"))
    system = captured["messages"][0]["content"]
    assert "可选口径" not in system
    assert "未确认" not in system


async def test_analyze_error_includes_confirmed_org_required_skill(tmp_path):
    """required 档已确认 org skill 注入 analyze_error system prompt。"""
    from trove.core.config import AgentConfig
    from trove.workflow.nodes.analyze_error import make_analyze_error

    svc = _org_skills(tmp_path)
    svc.create({
        "name": "org-diagnose", "description": "组织诊断口径",
        "triggers": {"node": "analyze_error"}, "tier": "required",
        "body": "## 组织诊断口径\n按失败类别分层处置",
    })
    svc.confirm("org-diagnose")

    captured = {}

    class LLM:
        async def chat(self, model, messages, **kwargs):
            captured.update(messages=messages)
            return "类型: Filter\n判断: 条件过严\n修正: 放宽\nTARGET: query_sketch"

    node = make_analyze_error(LLM(), AgentConfig(target="m"), skills=svc)
    await node(make_state(
        sql="SELECT * FROM loan",
        error_feedback="no rows",
        schema_context="Table: loan",
        lang="zh",
    ))
    assert "组织诊断口径" in captured["messages"][0]["content"]


async def test_load_skill_tool_registered_and_serves_confirmed_body(tmp_path):
    """gen_sql 注册表:有 confirmed available skill 时挂 load_skill 工具。"""
    from trove.workflow.nodes.gen_sql import build_sql_registry

    svc = _org_skills(tmp_path)
    svc.create({
        "name": "join-tricks", "description": "join 技巧",
        "tier": "available", "body": "## join 技巧\n先用小表驱动",
    })
    svc.confirm("join-tricks")

    registry = build_sql_registry(None, "q", "en", "sqlite", complexity="complex", skills=svc)
    names = {d["function"]["name"] for d in registry.defs()}
    assert "load_skill" in names
    handler = registry.handlers()["load_skill"]
    out = await handler({"skill_name": "join-tricks"})
    assert "小表驱动" in out


def test_load_skill_tool_absent_without_skills(tmp_path):
    """无 SkillService 或无可加载技能时不注册 load_skill(零工具噪声)。"""
    from trove.workflow.nodes.gen_sql import build_sql_registry

    registry = build_sql_registry(None, "q", "en", "sqlite", complexity="complex")
    assert "load_skill" not in {d["function"]["name"] for d in registry.defs()}

    svc = _org_skills(tmp_path)
    svc.create({
        "name": "pending-skill", "description": "未确认",
        "tier": "available", "body": "草稿正文",
    })
    registry = build_sql_registry(None, "q", "en", "sqlite", complexity="complex", skills=svc)
    assert "load_skill" not in {d["function"]["name"] for d in registry.defs()}


def test_available_skills_block_advertises_on_demand_skills(tmp_path):
    """available 档 skill 以描述广告,正文不常驻 prompt。"""
    svc = _org_skills(tmp_path)
    svc.create({
        "name": "opt-skill", "description": "可选 join 技巧",
        "tier": "available", "body": "## 很长的正文\n(不应出现在广告块里)",
    })
    svc.confirm("opt-skill")
    block = svc.available_skills_block("gen_sql", lang="zh")
    assert "load_skill(skill_name=\"opt-skill\")" in block
    assert "可选 join 技巧" in block
    assert "很长的正文" not in block
