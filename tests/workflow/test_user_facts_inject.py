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
