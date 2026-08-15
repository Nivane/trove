"""Error analysis node tests — classify, judge, and plan the fix."""

import pytest

from trove.core.config import AgentConfig
from trove.workflow.nodes.analyze_error import make_analyze_error
from trove.workflow.state import WorkflowState


def make_state(**kwargs):
    defaults = {"session_id": "s1", "question": "q"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


class TestAnalyzeError:
    async def test_produces_analysis_with_classification(self):
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(messages=messages, **kwargs)
                return "类型: Schema Linking\n判断: loans 表不存在\n修正: 改用 loan"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        state = make_state(
            sql="SELECT * FROM loans",
            error_feedback="no such table: loans",
            schema_context="Table: loan",
        )
        update = await node(state)
        assert "Schema Linking" in update["error_analysis"]
        # prompt 含错误 SQL、错误信息、schema 与分类要求
        prompt = " ".join(m["content"] for m in captured["messages"])
        assert "loans" in prompt
        assert "no such table" in prompt
        assert "Schema Linking" in prompt

    async def test_no_feedback_passes(self):
        class NoLLM:
            async def chat(self, *a, **k):
                raise AssertionError("must not run")

        node = make_analyze_error(NoLLM(), AgentConfig(target="m"))
        assert await node(make_state()) == {}

    async def test_llm_failure_is_silent(self):
        class BrokenLLM:
            async def chat(self, *a, **k):
                raise RuntimeError("down")

        node = make_analyze_error(BrokenLLM(), AgentConfig(target="m"))
        assert await node(make_state(error_feedback="boom")) == {}
