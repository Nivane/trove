"""Metadata answer validation tests — hallucination rules + LLM judge."""


from trove.workflow.nodes.answer import make_answer_metadata
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
        # 通过即显式清反馈:在途反馈若幸存到此就是路由打转的燃料
        assert await node(state) == {"error_feedback": ""}

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
        assert await node(state) == {"error_feedback": ""}

    async def test_issue_advances_retry_count(self):
        """复判必须自增预算 —— 冻结的 retry_count 让上限永远不可达(旧循环)。"""

        class JudgeLLM:
            async def chat(self, model, messages, **kwargs):
                return "ISSUE: 未回答完整"

        node = self._node(JudgeLLM(), connectors=self.FakeConnectors())
        state = make_state(intent_answer="loan.account_id 关联 account.account_id", retry_count=1)
        update = await node(state)
        assert update["retry_count"] == 2

    async def test_budget_exhausted_sets_error(self):
        """预算耗尽 = 终态 error(图把它路由到 output),不再发射反馈。"""

        class JudgeLLM:
            async def chat(self, model, messages, **kwargs):
                return "ISSUE: 未回答完整"

        node = make_metadata_check(self.FakeConnectors(), llm=JudgeLLM(), max_retries=2)
        state = make_state(intent_answer="loan.account_id 关联 account.account_id", retry_count=2)
        update = await node(state)
        assert "未回答完整" in update["error"]
        assert "error_feedback" not in update

    async def test_stale_feedback_is_reevaluated_and_cleared(self):
        """在途反馈不再短路本节点:复判 + 通过即清(否则路由拿着旧反馈回头)。"""
        calls = []

        class JudgeLLM:
            async def chat(self, model, messages, **kwargs):
                calls.append(messages)
                return "OK"

        node = self._node(JudgeLLM(), connectors=self.FakeConnectors())
        state = make_state(
            intent_answer="loan.account_id 关联 account.account_id",
            error_feedback="上一轮的裁决意见",
        )
        assert await node(state) == {"error_feedback": ""}
        assert len(calls) == 1  # 没有短路，真的复判了

    async def test_judge_sees_metadata_context_snapshot(self):
        """评审对照的是生成时那份上下文快照(system 第 2 条判据的前提)。"""
        seen = {}

        class JudgeLLM:
            async def chat(self, model, messages, **kwargs):
                seen["messages"] = messages
                return "OK"

        node = self._node(JudgeLLM(), connectors=self.FakeConnectors())
        state = make_state(
            intent_answer="loan.account_id 关联 account.account_id",
            metadata_context="Table loan: loan_id, account_id",
        )
        await node(state)
        assert "Table loan: loan_id, account_id" in seen["messages"][1]["content"]

    async def test_error_passthrough(self):
        node = self._node(None, connectors=self.FakeConnectors())
        state = make_state(intent_answer="x", error="upstream")
        assert await node(state) == {}


class TestAnswerMetadataFeedbackContract:
    """回路的另一端:answer_metadata 交付即消费反馈 + 留上下文快照。

    两端合起来才是终止条件:消费者清信号(check 的通过路径只是幂等兜底),
    预算在 check 手里,error 由图上直达 output。缺任何一端都会打转。
    """

    class StubLLM:
        async def chat(self, model, messages, **kwargs):
            return "students 表包含 id、name、grade 列。"

    async def test_success_clears_feedback_and_snapshots_context(self, sqlite_registry, catalog):
        node = make_answer_metadata(
            catalog=catalog, connectors=sqlite_registry, llm=self.StubLLM(),
        )
        state = make_state(question="有哪些表", error_feedback="上一轮裁决意见")
        out = await node(state)
        assert out["intent_answer"]
        assert out["error_feedback"] == ""  # 交付即消费掉在途反馈
        assert out["metadata_context"]  # 快照交给 check 对照评审

    async def test_fallback_delivery_clears_feedback_too(self, sqlite_registry, catalog):
        """无 LLM 凭证的模板回退同样是「本轮交付」:照清反馈、照留快照。"""
        node = make_answer_metadata(catalog=catalog, connectors=sqlite_registry)
        state = make_state(question="有哪些表", error_feedback="上一轮裁决意见")
        out = await node(state)
        assert out["intent_answer"]
        assert out["error_feedback"] == ""
        assert out["metadata_context"]
