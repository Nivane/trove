"""User facts → gen_sql prompt injection tests.

User-level memory (Mem0-style): facts scoped to (user_id, datasource) are
fetched by the gen_sql node and rendered as a "user facts" context block.
The asking user's own facts appear in the prompt; another user's facts and
another datasource's facts never do.
"""

from __future__ import annotations

import pytest

from trove.core.config import AgentConfig
from trove.services.datasource.catalog import CatalogService
from trove.services.user_facts.service import UserFactsService
from trove.workflow.graphs import GraphServices, build_graphs
from trove.workflow.nodes.gen_sql import build_sql_prompt, build_sql_prompt_from_state
from trove.workflow.state import GenSQLState, WorkflowState


def test_build_sql_prompt_renders_user_facts():
    prompt = build_sql_prompt(
        question="营收多少",
        schema_context="students (id, name, grade)",
        dialect="sqlite",
        user_facts=[{"fact": "营收 = 净收入"}],
    )
    assert "营收 = 净收入" in prompt
    assert "Your personal notes" in prompt


def test_build_sql_prompt_omits_user_facts_when_none():
    prompt = build_sql_prompt(
        question="营收多少",
        schema_context="students (id, name, grade)",
        dialect="sqlite",
    )
    assert "Your personal notes" not in prompt


def test_render_user_facts_matches_template_format():
    from trove.workflow.nodes.gen_sql import render_user_facts

    assert render_user_facts([{"fact": "a"}, {"fact": "b"}]) == "- a\n- b\n"


class TestUserFactsPromptInjection:
    async def test_user_facts_injected_into_gen_sql_prompt(
        self, tmp_path, sqlite_registry
    ):
        from tests.workflow.test_graphs import RecordingLLM

        svc = UserFactsService(tmp_path / "user_facts.db")
        await svc.add("local", "test_db", "营收 = 净收入")
        llm = RecordingLLM(["query", "```sql\nSELECT 1 FROM students;\n```", "OK"])
        services = GraphServices(
            llm=llm,
            catalog=CatalogService(sqlite_registry),
            connectors=sqlite_registry,
            config=AgentConfig(target="mock/model"),
            semantic_layer=getattr(sqlite_registry, "_test_semantic_provider", None),
            user_facts=svc,
        )
        graphs = build_graphs(services, multi_candidate=False, planner=False, agentic=False)

        state = WorkflowState(
            session_id="s1",
            question="平均成绩是多少",
            user_id="local",
            datasource="test_db",
            lang="zh",
        )
        await graphs["reflection"].ainvoke(state)

        prompts = [
            msg.get("content", "") for m in llm.calls for msg in m
            if "Database schema:" in str(msg.get("content", ""))
        ]
        assert prompts, f"no gen prompt; calls={len(llm.calls)}"
        assert any("营收 = 净收入" in p for p in prompts)

    async def test_user_facts_scoped_per_user_and_datasource(
        self, tmp_path, sqlite_registry
    ):
        from tests.workflow.test_graphs import RecordingLLM

        svc = UserFactsService(tmp_path / "user_facts.db")
        await svc.add("alice", "test_db", "alice 口径")
        await svc.add("bob", "test_db", "bob 口径")
        await svc.add("alice", "other", "alice 其它库")
        llm = RecordingLLM(["query", "```sql\nSELECT 1 FROM students;\n```", "OK"])
        services = GraphServices(
            llm=llm,
            catalog=CatalogService(sqlite_registry),
            connectors=sqlite_registry,
            config=AgentConfig(target="mock/model"),
            semantic_layer=getattr(sqlite_registry, "_test_semantic_provider", None),
            user_facts=svc,
        )
        graphs = build_graphs(services, multi_candidate=False, planner=False, agentic=False)

        state = WorkflowState(
            session_id="s1",
            question="平均成绩是多少",
            user_id="alice",
            datasource="test_db",
            lang="zh",
        )
        await graphs["reflection"].ainvoke(state)

        prompts = [
            msg.get("content", "") for m in llm.calls for msg in m
            if "Database schema:" in str(msg.get("content", ""))
        ]
        assert prompts
        joined = "\n".join(prompts)
        assert "alice 口径" in joined
        assert "bob 口径" not in joined
        assert "alice 其它库" not in joined


def test_build_sql_prompt_from_state_carries_user_facts():
    state = GenSQLState(
        question="q", schema_context="s", dialect="sqlite",
        user_facts=[{"fact": "我的口径"}],
    )
    prompt = build_sql_prompt_from_state(state)
    assert "我的口径" in prompt


# ── ⑦ profile_boost:失败画像提示块 ───────────────────────


def test_render_profile_formats_patterns_and_rate():
    from trove.workflow.nodes.gen_sql import render_profile
    text = render_profile({
        "totals": {"total": 4, "ok": 1},
        "ok_rate": 0.25,
        "failure_patterns": [
            {"pattern": "missing approved filter", "count": 2},
            {"pattern": "wrong region code", "count": 1},
        ],
    })
    assert "25% (1/4)" in text
    assert "missing approved filter (x2)" in text
    assert "wrong region code (x1)" in text
    # 用户级信号而非问题级:纯文本行,无每问题 SQL
    assert "SQL:" not in text


def test_render_profile_empty_without_patterns():
    from trove.workflow.nodes.gen_sql import render_profile
    # 空画像(无失败模式)→ 空串:调用方不注入,零噪音零成本
    assert render_profile({"totals": {"total": 2, "ok": 2}, "ok_rate": 1.0,
                           "failure_patterns": []}) == ""
    assert render_profile({}) == ""


def test_build_sql_prompt_renders_profile_section():
    from trove.workflow.nodes.gen_sql import render_profile
    text = render_profile({
        "totals": {"total": 4, "ok": 1}, "ok_rate": 0.25,
        "failure_patterns": [{"pattern": "missing approved filter", "count": 2}],
    })
    prompt = build_sql_prompt(
        question="q", schema_context="s", dialect="sqlite",
        profile=text,
    )
    assert "repeated failure patterns" in prompt
    assert "missing approved filter" in prompt


def test_build_sql_prompt_omits_profile_when_empty():
    prompt = build_sql_prompt(
        question="q", schema_context="s", dialect="sqlite",
        profile="",
    )
    assert "repeated failure patterns" not in prompt


def test_build_sql_prompt_from_state_carries_profile():
    from trove.workflow.nodes.gen_sql import render_profile
    state = GenSQLState(
        question="q", schema_context="s", dialect="sqlite",
        profile=render_profile({
            "totals": {"total": 4, "ok": 1}, "ok_rate": 0.25,
            "failure_patterns": [{"pattern": "missing approved filter", "count": 2}],
        }),
    )
    prompt = build_sql_prompt_from_state(state)
    assert "missing approved filter" in prompt


def test_from_workflow_gates_profile_by_included():
    """预算未选中 profile 块 → 空串(不注入);选中 → 透传。"""
    wf = WorkflowState(question="q", session_id="s1")
    st = GenSQLState.from_workflow(wf, dialect="sqlite", included=set(),
                                   profile="PROFILE_TEXT")
    assert st.profile == ""
    st2 = GenSQLState.from_workflow(
        wf, dialect="sqlite", included={"profile"}, profile="PROFILE_TEXT")
    assert st2.profile == "PROFILE_TEXT"
