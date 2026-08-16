"""Consensus selection node tests — multi-candidate result agreement."""

import pytest

from trove.core.types import QueryResult
from trove.workflow.nodes.select import _normalize_rows, make_select_consensus
from trove.workflow.state import WorkflowState


def make_state(**kwargs):
    defaults = {"session_id": "s1", "question": "q"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


class FakeConnectors:
    def __init__(self, results):
        self._results = list(results)
        self.executed = []

    async def execute(self, sql, datasource=None):
        self.executed.append(sql)
        return self._results.pop(0)


class TestNormalizeRows:
    def test_order_and_type_insensitive(self):
        assert _normalize_rows([[1, "a"], [2, "b"]]) == _normalize_rows([["2", "b"], ["1", "a"]])

    def test_different_rows_differ(self):
        assert _normalize_rows([[1]]) != _normalize_rows([[2]])


class TestSelectNode:
    async def test_no_candidates_passes(self):
        node = make_select_consensus(FakeConnectors([]))
        update = await node(make_state(rows=[[1]], row_count=1))
        assert update == {}
        assert FakeConnectors([]).executed == []

    async def test_consensus_passes(self):
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[1], [2]], row_count=2),
        ])
        node = make_select_consensus(connectors)
        state = make_state(
            sql="SELECT v FROM t", rows=[[1], [2]], row_count=2,
            candidates=["SELECT v FROM t ORDER BY v"],
        )
        assert await node(state) == {}
        assert connectors.executed == ["SELECT v FROM t ORDER BY v"]

    async def test_disagreement_feeds_back(self):
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[1]], row_count=1),
        ])
        node = make_select_consensus(connectors)
        state = make_state(
            sql="SELECT v FROM t", rows=[[1], [2]], row_count=2,
            candidates=["SELECT v FROM t WHERE 0"], lang="en",
        )
        update = await node(state)
        assert "error" not in update
        assert "different results" in update["error_feedback"]
        assert update["retry_count"] == 1
        assert update["consensus"] is False  # 分歧即标记，不只耗尽时

    async def test_disagreement_feedback_carries_candidates_and_values(self):
        """反馈必须给出双方 SQL 与结果值,让重试有据可依(而非一句 unstable)。"""
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[33]], row_count=1),
        ])
        node = make_select_consensus(connectors)
        state = make_state(
            sql="SELECT COUNT(*) FROM account a WHERE a.frequency = 'POPLATEK PO OBRATU'",
            rows=[[45]], row_count=1,
            candidates=["SELECT COUNT(*) FROM account a JOIN card c ON a.account_id = c.account_id WHERE c.issued > a.date"],
        )
        update = await node(state)
        fb = update["error_feedback"]
        assert "POPLATEK PO OBRATU" in fb  # 主候选 SQL
        assert "c.issued" in fb           # 备选 SQL
        assert "45" in fb and "33" in fb  # 双方结果值
        assert "候选 SQL 结果不一致" in fb  # 默认中文反馈

    async def test_disagreement_budget_exhausted_marks_low_confidence(self):
        """预算耗尽不再硬降级：放行主候选 + 低置信标记。"""
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[1]], row_count=1),
        ])
        node = make_select_consensus(connectors)
        state = make_state(
            sql="SELECT v FROM t", rows=[[1], [2]], row_count=2,
            candidates=["SELECT v FROM t WHERE 0"], retry_count=10,
        )
        update = await node(state)
        assert update == {"consensus": False}
        assert "error" not in update

    async def test_alt_execution_failure_silently_passes(self):
        class Exploding:
            async def execute(self, sql, datasource=None):
                raise RuntimeError("boom")

        node = make_select_consensus(Exploding())
        state = make_state(
            rows=[[1]], row_count=1, candidates=["SELECT v FROM t"],
        )
        assert await node(state) == {}

    async def test_pending_feedback_passes_through(self):
        node = make_select_consensus(FakeConnectors([]))
        state = make_state(error_feedback="pending")
        assert await node(state) == {}
