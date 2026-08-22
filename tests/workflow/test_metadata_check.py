"""Metadata answer validation tests — hallucination rules + LLM judge."""

import pytest

from trove.workflow.nodes.metadata_check import (
    find_hallucinations,
    make_metadata_check,
)
from trove.workflow.state import WorkflowState


def make_state(**kwargs):
    defaults = {"session_id": "s1", "question": "q"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


SCHEMA = {
    "district": ["district_id", "name"],
    "loan": ["loan_id", "account_id"],
    "order": ["order_id", "account_id"],
    "account": ["account_id"],
}


class TestHallucinationRules:
    def test_valid_references_pass(self):
        answer = "loan.account_id 关联 account.account_id"
        assert find_hallucinations(answer, SCHEMA) == []

    def test_unknown_table_flagged(self):
        assert find_hallucinations("ghost.id 关联 loan.account_id", SCHEMA) == ["ghost.id"]

    def test_unknown_column_flagged(self):
        assert find_hallucinations("loan.amount 关联 account.account_id", SCHEMA) == ["loan.amount"]

    def test_plain_text_passes(self):
        assert find_hallucinations("这两个表通过 account_id 关联", SCHEMA) == []


class TestMetadataCheckNode:
    def _node(self, llm, connectors=None):
        return make_metadata_check(connectors, llm=llm)

    class FakeConnectors:
        async def get_schema(self, datasource=None):
            from trove.core.types import SchemaInfo, TableInfo, ColumnInfo
            return SchemaInfo(tables=[
                TableInfo(name="loan", columns=[ColumnInfo(name="loan_id", type="int"), ColumnInfo(name="account_id", type="int")]),
                TableInfo(name="account", columns=[ColumnInfo(name="account_id", type="int")]),
            ])

    async def test_hallucination_feeds_back(self):
        class NoLLM:
            async def chat(self, *a, **k):
                raise AssertionError("judge must not run on rule failure")

        node = self._node(NoLLM(), connectors=self.FakeConnectors())
        state = make_state(intent_answer="ghost.id 关联 loan.account_id")
        update = await node(state)
        assert "ghost.id" in update["error_feedback"]
        assert update["retry_count"] == 1

    async def test_clean_answer_goes_to_llm_judge_ok(self):
        class JudgeLLM:
            async def chat(self, model, messages, **kwargs):
                return "OK"

        node = self._node(JudgeLLM(), connectors=self.FakeConnectors())
        state = make_state(intent_answer="loan.account_id 关联 account.account_id")
        assert await node(state) == {}

    async def test_llm_judge_issue_feeds_back(self):
        class JudgeLLM:
            async def chat(self, model, messages, **kwargs):
                return "ISSUE: 未回答完整"

        node = self._node(JudgeLLM(), connectors=self.FakeConnectors())
        state = make_state(intent_answer="loan.account_id 关联 account.account_id")
        update = await node(state)
        assert "未回答完整" in update["error_feedback"]

    async def test_llm_judge_failure_passes(self):
        class BrokenLLM:
            async def chat(self, *a, **k):
                raise RuntimeError("down")

        node = self._node(BrokenLLM(), connectors=self.FakeConnectors())
        state = make_state(intent_answer="loan.account_id 关联 account.account_id")
        assert await node(state) == {}

    async def test_error_passthrough(self):
        node = self._node(None, connectors=self.FakeConnectors())
        state = make_state(intent_answer="x", error="upstream")
        assert await node(state) == {}
