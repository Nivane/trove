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
        if "boom" in sql:
            raise RuntimeError("boom")
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
        assert update["consensus"] is False
        assert "error" not in update
        assert update["selection"]["adopted"] is False

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


class TestVoteSelection:
    """多候选执行结果分组投票:多数派胜出,平局打回,异常候选过滤。"""

    def _state(self, **kw):
        defaults = dict(
            question="list the accounts", sql="SELECT id FROM t",
            rows=[[1], [2]], row_count=2, candidates=[],
        )
        defaults.update(kw)
        return make_state(**defaults)

    async def test_majority_matches_primary_passes(self):
        """多数派(3 票)与 primary 结果一致 → 无操作。"""
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[1], [2]], row_count=2),  # 候选 A: 同 primary
            QueryResult(columns=["v"], rows=[[1], [2]], row_count=2),  # 候选 B: 同 primary
            QueryResult(columns=["v"], rows=[[1], [2]], row_count=2),  # 候选 C: 同 primary
            QueryResult(columns=["v"], rows=[[9]], row_count=1),       # 候选 D: 少数派
        ])
        node = make_select_consensus(connectors)
        update = await node(self._state(candidates=[f"SELECT id FROM t WHERE {i}" for i in range(4)]))
        assert update == {}

    async def test_majority_not_primary_adopts_winner(self):
        """多数派不含 primary → 采纳多数派 SQL 及其执行结果(不重跑)。"""
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[7]], row_count=1),  # A
            QueryResult(columns=["v"], rows=[[7]], row_count=1),  # B
            QueryResult(columns=["v"], rows=[[7]], row_count=1),  # C
            QueryResult(columns=["v"], rows=[[1], [2]], row_count=2),  # D: 同 primary
        ])
        node = make_select_consensus(connectors)
        update = await node(self._state(
            sql="SELECT id FROM t",
            candidates=["SELECT A", "SELECT B", "SELECT C", "SELECT D"],
        ))
        assert "error_feedback" not in update
        assert "SELECT A" in update["sql"]
        assert update["rows"] == [[7]]
        assert update["row_count"] == 1
        assert update["consensus"] is True
        assert update["selection"]["adopted"] is True

    async def test_everyone_single_vote_feeds_back(self):
        """全平局(每个候选结果都不同) → 打回重生成。"""
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[7]], row_count=1),
            QueryResult(columns=["v"], rows=[[8]], row_count=1),
            QueryResult(columns=["v"], rows=[[9]], row_count=1),
            QueryResult(columns=["v"], rows=[[10]], row_count=1),
        ])
        node = make_select_consensus(connectors)
        update = await node(self._state(
            lang="en",
            candidates=["SELECT 1", "SELECT 2", "SELECT 3", "SELECT 4"],
        ))
        assert "different results" in update["error_feedback"]
        assert update["retry_count"] == 1
        assert update["consensus"] is False

    async def test_repeated_ties_degrade_before_budget(self):
        """任务4: 平局轮次达阈值 → 提前降级交付 primary,不再打回拉锯。

        平局 = 无唯一多数派(并列或全单票)——此时没有"票王"可采纳,
        止损动作 = 预算耗尽分支的保守交付 + degraded 标记。
        """
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[1]], row_count=1),  # A
            QueryResult(columns=["v"], rows=[[1]], row_count=1),  # B（与 A 并列 2:2）
            QueryResult(columns=["v"], rows=[[2]], row_count=1),  # C
            QueryResult(columns=["v"], rows=[[2]], row_count=1),  # D（与 C 并列 2:2）
        ])
        node = make_select_consensus(connectors, adopt_after_tie_rounds=3)
        state = self._state(
            rows=[[0]], row_count=1,  # primary 独票
            candidates=["SELECT A", "SELECT B", "SELECT C", "SELECT D"],
            tie_rounds=3,  # 已拉锯 3 轮
        )
        update = await node(state)
        assert "error_feedback" not in update  # 不再打回
        assert update["consensus"] is False    # 低置信交付
        assert update["selection"]["adopted"] is False  # 保守保留 primary
        assert update["selection"]["winner"] == "primary"
        assert update["selection"]["degraded"] == "repeated-tie"

    async def test_ties_below_threshold_still_feed_back(self):
        """平局轮次未达阈值 → 照常打回重生成。"""
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[7]], row_count=1),
            QueryResult(columns=["v"], rows=[[8]], row_count=1),
        ])
        node = make_select_consensus(connectors, adopt_after_tie_rounds=3)
        state = self._state(
            rows=[[0]], row_count=1,
            candidates=["SELECT 1", "SELECT 2"],
            tie_rounds=2, lang="en",
        )
        update = await node(state)
        assert "different results" in update["error_feedback"]
        assert update["retry_count"] == 1
        assert "degraded" not in update["selection"]

    async def test_split_majority_tie_below_threshold_feeds_back(self):
        """并列平局(2:2)未达阈值 → 同样打回(候选组进反馈)。"""
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[1]], row_count=1),  # A
            QueryResult(columns=["v"], rows=[[1]], row_count=1),  # B
            QueryResult(columns=["v"], rows=[[2]], row_count=1),  # C
            QueryResult(columns=["v"], rows=[[2]], row_count=1),  # D
        ])
        node = make_select_consensus(connectors, adopt_after_tie_rounds=3)
        state = self._state(
            rows=[[0]], row_count=1,  # primary 独票
            candidates=["SELECT A", "SELECT B", "SELECT C", "SELECT D"],
            tie_rounds=2, lang="en",
        )
        update = await node(state)
        assert "different results" in update["error_feedback"]
        assert update["retry_count"] == 1
        assert update["tie_rounds"] == 3  # 平局计数 +1

    async def test_split_2_2_feeds_back(self):
        """两组并列 2:2(primary 组 2 票 vs 候选组 2 票 + 单票组) → 平局打回。"""
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[1], [2]], row_count=2),  # A: 同 primary
            QueryResult(columns=["v"], rows=[[7]], row_count=1),       # B
            QueryResult(columns=["v"], rows=[[7]], row_count=1),       # C
            QueryResult(columns=["v"], rows=[[9]], row_count=1),       # D: 单票
        ])
        node = make_select_consensus(connectors)
        update = await node(self._state(candidates=["SELECT A", "SELECT B", "SELECT C", "SELECT D"]))
        assert "error_feedback" in update

    async def test_invalid_candidate_dropped_from_vote(self):
        """被 verify 规则拦下的候选(如单值题返回多行)不参与投票。"""
        q = "how many accounts have running contracts"
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[1], [2]], row_count=2),   # A: 被 F1-a 拦
            QueryResult(columns=["v"], rows=[[5]], row_count=1),        # B: 与 primary 一致
        ])
        node = make_select_consensus(connectors)
        state = self._state(
            question=q, rows=[[5]], row_count=1, candidates=["SELECT A", "SELECT B"],
        )
        update = await node(state)
        assert update == {}  # 过滤后 primary(1) + B(1) 一致 → 通过
        assert update == {}

    async def test_all_candidates_filtered_passes(self):
        """有效候选 0 个(规则拦截 + 执行失败)→ 静默保留 primary。"""
        q = "list the top ten withdrawals"
        connectors = FakeConnectors([
            QueryResult(columns=["a", "b", "c"], rows=[[1, 2, 3]], row_count=1),  # A: F1-b 拦(3 列)
        ])
        node = make_select_consensus(connectors)
        state = self._state(
            question=q, rows=[[3], [4]], row_count=2,
            candidates=["SELECT 1", "SELECT boom"],
        )
        assert await node(state) == {}

    async def test_majority_winner_adopts_with_filtered_report(self):
        """采纳多数派时,selection 详情记录分组与过滤痕迹(eval 归因)。"""
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[7]], row_count=1),
            QueryResult(columns=["v"], rows=[[7]], row_count=1),
            QueryResult(columns=["v"], rows=[[7]], row_count=1),
        ])
        node = make_select_consensus(connectors)
        update = await node(self._state(candidates=["SELECT A", "SELECT B", "SELECT C"]))
        sel = update["selection"]
        assert list(sel["votes"].values()) == [3, 1]  # 多数派 3 票 + primary 1 票
        assert sel["adopted"] is True
        assert "filtered" in sel and len(sel["filtered"]) == 0

    async def test_execution_failure_dropped_from_vote(self):
        """执行失败的候选丢弃,其余照常投票。"""
        class OneBoom:
            def __init__(self):
                self.executed = []

            async def execute(self, sql, datasource=None):
                self.executed.append(sql)
                if "boom" in sql:
                    raise RuntimeError("boom")
                return QueryResult(columns=["v"], rows=[[1], [2]], row_count=2)

        connectors = OneBoom()
        node = make_select_consensus(connectors)
        update = await node(self._state(candidates=["SELECT boom", "SELECT ok"]))
        assert update == {}
        assert connectors.executed == ["SELECT boom", "SELECT ok"]


class TestConfidence:
    """缺口5: 候选投票分布 → 置信度信号（票王得票率,注入 selection 供观测）。"""

    async def test_majority_confidence_is_winner_share(self):
        """多数派 2 票 + primary 独票 → 置信度 2/3。"""
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[7]], row_count=1),  # A
            QueryResult(columns=["v"], rows=[[7]], row_count=1),  # B
        ])
        node = make_select_consensus(connectors)
        update = await node(make_state(
            sql="SELECT id FROM t", rows=[[1], [2]], row_count=2,
            candidates=["SELECT A", "SELECT B"],
        ))
        assert update["consensus"] is True
        assert update["selection"]["adopted"] is True
        assert update["selection"]["confidence"] == pytest.approx(2 / 3)

    async def test_budget_exhausted_confidence_marks_low(self):
        """预算耗尽降级交付 primary → 仍带置信度(票王占比),供输出方观测。"""
        connectors = FakeConnectors([
            QueryResult(columns=["v"], rows=[[1]], row_count=1),
        ])
        node = make_select_consensus(connectors)
        update = await node(make_state(
            sql="SELECT v FROM t", rows=[[1], [2]], row_count=2,
            candidates=["SELECT v FROM t WHERE 0"], retry_count=10,
        ))
        assert update["selection"]["confidence"] == pytest.approx(1 / 2)
