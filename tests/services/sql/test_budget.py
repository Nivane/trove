"""执行画像 P1（设计 §5.2 / §7.1）：预算降级链与三路径决策。

这个文件的重点**不是**「估算得准不准」，而是**估算不出来时往哪边倒**。

存量三道成本护栏（EXPLAIN 解析 / 方言支持 / 估算可得）全在往「放行」倒，而成本
轨**没有下层兜底**——只读角色不会阻止一条扫 10TB 的 SELECT。设计 §8.1 的判据是
「有没有兜底」而不是「哪个更危险」：有兜底的权限轨可以 fail-open，没兜底的成本轨
必须 fail-closed。

因此这里逐条钉住 §4 的不变量：

* **I2** 不得静默 fail-open —— 任何一次「无法估算」都要留下 ``degraded`` 痕迹
* **I3** 估算失败时默认方向是保守
* **I6** 护栏自身故障不阻断查询 —— 走保守预算，不 raise 到用户
"""

from __future__ import annotations

import pytest

from trove.core.types import QueryResult
from trove.services.sql.budget import (
    BudgetService,
    CostEstimate,
    ExecutionBudget,
    force_limit,
)


def _qresult(rows, columns=("rows",)):
    return QueryResult(columns=list(columns), rows=rows, row_count=len(rows),
                       execution_time_ms=1)


BUDGET = ExecutionBudget(
    max_rows=1000,
    timeout_ms=30_000,
    soft_scan_rows=50_000_000,
    hard_scan_rows=1_000_000_000,
)


# ── decide:三路径决策 ─────────────────────────────────────


class TestDecide:
    def test_estimate_under_soft_is_allowed(self):
        est = CostEstimate(estimated_rows=1_000, estimated_bytes=None,
                           source="explain", degraded=False, detail={})
        d = BudgetService().decide(est, BUDGET)
        assert d.verdict == "allow"
        assert d.over == ""

    def test_estimate_at_soft_cap_is_allowed(self):
        """边界是**闭区间**:等于 soft 不算超。"""
        est = CostEstimate(BUDGET.soft_scan_rows, None, "explain", False, {})
        assert BudgetService().decide(est, BUDGET).verdict == "allow"

    def test_estimate_over_soft_is_rejected_for_regeneration(self):
        """超软限 ≠ 直接拒:打回 gen_sql 收窄(现有行为,§8.5 保留双上限)。"""
        est = CostEstimate(BUDGET.soft_scan_rows + 1, None, "explain", False, {})
        d = BudgetService().decide(est, BUDGET)
        assert d.verdict == "reject"
        assert d.over == "soft"

    def test_estimate_over_hard_is_rejected_outright(self):
        """超硬限不烧 LLM 重生成——重写十遍也降不到硬限以下。"""
        est = CostEstimate(BUDGET.hard_scan_rows + 1, None, "explain", False, {})
        d = BudgetService().decide(est, BUDGET)
        assert d.verdict == "reject"
        assert d.over == "hard"

    def test_unestimable_degrades_instead_of_allowing(self):
        """路径 3 是本方案改的**唯一**一处行为:放行 → 降级执行。

        存量在这里返回 None 然后一路放行,零成本上限。
        """
        est = CostEstimate(None, None, "conservative", True, {})
        d = BudgetService().decide(est, BUDGET)
        assert d.verdict == "degrade"
        assert d.reason

    def test_unestimable_can_be_configured_to_reject(self):
        """§8.3 C:过严的护栏会被绕过(用户去直连库),所以默认 degrade;
        但严格环境要能选 reject。"""
        strict = ExecutionBudget(on_unestimable="reject")
        est = CostEstimate(None, None, "conservative", True, {})
        assert BudgetService().decide(est, strict).verdict == "reject"

    def test_unknown_on_unestimable_is_rejected_at_construction(self):
        """配置值拼错 → 当场炸,不静默退回某个方向。

        静默退回哪一边都是错的:退回 degrade 是放宽了一个本该严格的环境,
        退回 reject 是让一个本该降级的环境每次估算失败都拒绝服务。
        """
        with pytest.raises(ValueError):
            ExecutionBudget(on_unestimable="silent")

    def test_zero_or_missing_estimate_is_treated_as_unestimable(self):
        """``estimated_rows`` 缺失就走保守路径 —— 不看 ``source`` 字段自证。

        ``source`` 是给人读的;判定只认「有没有数」。两者若不一致,信数。
        """
        est = CostEstimate(None, None, "metadata", False, {})
        assert BudgetService().decide(est, BUDGET).verdict == "degrade"


# ── estimate:降级链 ───────────────────────────────────────


class TestDegradationChain:
    async def test_explain_wins_when_available(self):
        svc = BudgetService(
            explain=lambda sql, ds=None: _qresult([(5000,)]),
            parse_explain=lambda dialect, res: 5000,
            metadata=lambda tables: 10**9,  # 粗估算,不该被采用
        )
        est = await svc.estimate("db", "SELECT 1", "postgres", ["t"])
        assert est.source == "explain"
        assert est.estimated_rows == 5000
        assert est.degraded is False

    async def test_falls_to_metadata_when_explain_unparsable(self):
        """EXPLAIN 拿得到但解析不出(方言盲区)→ 退到元数据画像,不是放行。"""
        svc = BudgetService(
            explain=lambda sql, ds=None: _qresult([("Seq Scan on t",)]),
            parse_explain=lambda dialect, res: None,
            metadata=lambda tables: 2_000_000,
        )
        est = await svc.estimate("db", "SELECT 1", "postgres", ["t"])
        assert est.source == "metadata"
        assert est.estimated_rows == 2_000_000
        assert est.degraded is False

    async def test_falls_to_conservative_when_nothing_is_available(self):
        svc = BudgetService()  # 无 explain、无画像
        est = await svc.estimate("db", "SELECT 1", "postgres", ["t"])
        assert est.source == "conservative"
        assert est.estimated_rows is None
        assert est.degraded is True
        assert est.estimated_bytes == BUDGET.assume_max_scan_bytes

    async def test_metadata_path_is_skipped_when_no_tables_are_known(self):
        """表名都不认识 → 画像无从查起 → 保守预算。别去查一个空表清单。"""
        called = []
        svc = BudgetService(
            metadata=lambda tables: called.append(tables) or 1,
        )
        est = await svc.estimate("db", "SELECT 1", "postgres", [])
        assert est.source == "conservative"
        assert called == []

    async def test_metadata_returning_zero_is_not_a_basis(self):
        """画像返回 0 不等于「这张表没有数据」——多半是统计信息没收集。

        当成 0 会让一条扫全表的查询被判成零成本,方向恰好是**放宽**。
        """
        svc = BudgetService(metadata=lambda tables: 0)
        est = await svc.estimate("db", "SELECT 1", "postgres", ["t"])
        assert est.source == "conservative"
        assert est.degraded is True

    async def test_explain_failure_falls_through_not_raises(self):
        """I6:护栏自身故障不阻断查询 —— 退到下一档,不 raise 到用户。"""
        def boom(sql, ds=None):
            raise RuntimeError("EXPLAIN not supported")

        svc = BudgetService(explain=boom, metadata=lambda tables: 42)
        est = await svc.estimate("db", "SELECT 1", "postgres", ["t"])
        assert est.source == "metadata"

    async def test_every_guard_failing_still_yields_an_estimate(self):
        """I6 + I2:全挂也要给出一个**保守**结论,而不是异常、也不是放行。"""
        def boom(*a, **k):
            raise RuntimeError("everything is on fire")

        svc = BudgetService(explain=boom, metadata=boom)
        est = await svc.estimate("db", "SELECT 1", "postgres", ["t"])
        assert est.source == "conservative"
        assert est.degraded is True

    async def test_parse_failure_of_untrusted_shape_does_not_raise(self):
        """解析器拿到脏形状(适配器换实现)时也不能炸穿。"""
        svc = BudgetService(
            explain=lambda sql, ds=None: _qresult([(1,)]),
            parse_explain=lambda dialect, res: (_ for _ in ()).throw(TypeError("nope")),
        )
        est = await svc.estimate("db", "SELECT 1", "postgres", ["t"])
        assert est.degraded is True


# ── 保守预算的数值来自配置 ────────────────────────────────


class TestConservativeBudgetComesFromConfig:
    async def test_assume_max_scan_bytes_is_the_configured_value(self):
        """I3:保守预算按**数据源可配**——20GB 只是默认(市场 execution profile)。"""
        budget = ExecutionBudget(assume_max_scan_bytes=5 * 1024**3)
        svc = BudgetService(budget=budget)
        est = await svc.estimate("db", "SELECT 1", "postgres", ["t"])
        assert est.estimated_bytes == 5 * 1024**3

    def test_budget_defaults_match_the_market_profile(self):
        b = ExecutionBudget()
        assert b.timeout_ms == 30_000
        assert b.soft_scan_rows == 50_000_000
        assert b.hard_scan_rows == 1_000_000_000
        assert b.assume_max_scan_bytes == 20 * 1024**3


# ── force_limit ───────────────────────────────────────────


class TestForceLimit:
    def test_adds_limit_to_bare_select(self):
        out = force_limit("SELECT a FROM t", "postgres", 1000)
        assert "LIMIT 1000" in out.sql.upper()
        assert out.limit == 1000

    def test_keeps_the_tighter_of_the_two(self):
        """已有 LIMIT 10 时不该被放宽到 1000 —— 那是把降级变成扩权。"""
        out = force_limit("SELECT a FROM t LIMIT 10", "postgres", 1000)
        assert "1000" not in out.sql
        # 生效的是**已有的那个**,不是我们想加的那个:记 1000 会让运维以为
        # 这条查询被放到了 1000 行,而实际只有 10 行。
        assert out.limit == 10

    def test_tightens_a_looser_existing_limit(self):
        out = force_limit("SELECT a FROM t LIMIT 999999", "postgres", 1000)
        assert "1000" in out.sql
        assert "999999" not in out.sql
        assert out.limit == 1000

    def test_unparseable_sql_returns_none(self):
        """加不上 LIMIT 时必须**能被告知** —— 返回原串会让调用方以为加上了。

        静默返回原文是这里最危险的写法:调用方记 degraded、却没有任何边界。
        """
        assert force_limit("SELECT FROM WHERE ((", "postgres", 1000) is None

    def test_empty_sql_returns_none(self):
        assert force_limit("", "postgres", 1000) is None
