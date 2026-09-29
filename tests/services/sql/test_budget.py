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

from trove.core.types import QueryResult, TableProfile
from trove.services.sql.budget import (
    BudgetService,
    CostEstimate,
    ExecutionBudget,
    describe_cost,
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


class TestCoarseEstimatesCannotFeedBack:
    """画像档(路径 2)的估算**只能降级,不能打回重生成**。

    §5.2 的处置表把「soft < est ≤ hard」一律写作 reject + 打回 gen_sql 加
    LIMIT。那条规则对 EXPLAIN 档成立:加了 LIMIT 计划会变,下一轮估算更小,
    是一个**收敛**的环。

    对画像档不成立。画像按**表行数求和**,看不见 WHERE、看不见投影、更看不见
    LIMIT —— ``SELECT name FROM big_table LIMIT 10`` 与不带 LIMIT 的估算是
    **同一个数**。于是打回重生成之后下一轮判定完全相同,再打回……直到烧完
    ``max_retries``。在 ClickHouse(恰恰是最需要护栏的大表方言)上,这意味着
    每个查询白烧十轮 LLM 再失败。

    硬限不受这条约束:它是**终局**(不重生成),没有环可谈。
    """

    def test_metadata_over_soft_degrades_instead_of_rejecting(self):
        est = CostEstimate(BUDGET.soft_scan_rows + 1, None, "metadata", False, {})
        d = BudgetService().decide(est, BUDGET)
        assert d.verdict == "degrade"
        assert d.over == ""
        assert d.reason

    def test_metadata_over_hard_still_rejects(self):
        """超过硬限时粗估算也是决定性的 —— 1B 行这个量级不需要精确。"""
        est = CostEstimate(BUDGET.hard_scan_rows + 1, None, "metadata", False, {})
        d = BudgetService().decide(est, BUDGET)
        assert d.verdict == "reject"
        assert d.over == "hard"

    def test_metadata_under_soft_is_allowed_without_a_limit(self):
        """路径 2 真正买到的东西:小表查询**不再被降级**。

        存量(与 P1 之后)这两个方言每条查询都落保守预算 → degraded 恒为真,
        degraded 从信号退化成噪声。有了画像,小表就是干净放行。
        """
        est = CostEstimate(1_000, None, "metadata", False, {})
        d = BudgetService().decide(est, BUDGET)
        assert d.verdict == "allow"
        assert d.over == ""

    def test_explain_over_soft_is_unaffected(self):
        """EXPLAIN 档保留打回重生成 —— 那是现有行为(§8.5 保留双上限)。"""
        est = CostEstimate(BUDGET.soft_scan_rows + 1, None, "explain", False, {})
        assert BudgetService().decide(est, BUDGET).verdict == "reject"

    def test_metadata_degrades_even_though_it_has_a_basis(self):
        """「有依据」与「要降级」不矛盾 —— ``degraded`` 说的是**有没有依据**。

        画像给了依据,所以 ``degraded=False``;但它粗到只能当地板用,所以要加
        LIMIT。两个字段各说各的事,调用方靠 ``limit_applied`` 知道边界在哪。
        """
        est = CostEstimate(BUDGET.soft_scan_rows + 1, None, "metadata", False, {})
        assert est.degraded is False
        assert BudgetService().decide(est, BUDGET).verdict == "degrade"


# ── estimate:降级链 ───────────────────────────────────────


class TestDegradationChain:
    async def test_explain_wins_when_available(self):
        svc = BudgetService(
            explain=lambda sql, ds=None: _qresult([(5000,)]),
            parse_explain=lambda dialect, res: 5000,
            metadata=lambda ds, tables: 10**9,  # 粗估算,不该被采用
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
            metadata=lambda ds, tables: 2_000_000,
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

    async def test_metadata_is_asked_for_the_current_datasource(self):
        """画像要**带数据源**问(与 ``explain`` 同参)。

        少了它,多数据源部署里画像查的是默认库:行数来自 A 库、SQL 跑在 B 库上,
        估算与执行对不上,而两边的日志都看不出这件事 —— 典型的安静错判。
        """
        seen: list[tuple[str, list[str]]] = []

        def metadata(datasource, tables):
            seen.append((datasource, list(tables)))
            return 1

        svc = BudgetService(metadata=metadata)
        await svc.estimate("warehouse_b", "SELECT 1", "postgres", ["t"])
        assert seen == [("warehouse_b", ["t"])]

    async def test_metadata_returning_zero_is_not_a_basis(self):
        """画像返回 0 不等于「这张表没有数据」——多半是统计信息没收集。

        当成 0 会让一条扫全表的查询被判成零成本,方向恰好是**放宽**。
        """
        svc = BudgetService(metadata=lambda ds, tables: 0)
        est = await svc.estimate("db", "SELECT 1", "postgres", ["t"])
        assert est.source == "conservative"
        assert est.degraded is True

    async def test_explain_failure_falls_through_not_raises(self):
        """I6:护栏自身故障不阻断查询 —— 退到下一档,不 raise 到用户。"""
        def boom(sql, ds=None):
            raise RuntimeError("EXPLAIN not supported")

        svc = BudgetService(explain=boom, metadata=lambda ds, tables: 42)
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


# ── describe_cost:生成前先问价(§7.2 / P3)──────────────────


class _CostProfiles:
    """画像替身:只实现 ``describe_cost`` 用的那一面(``table_profile``)。

    断言「问的是**这一轮**的数据源」——多数据源部署里漏掉它,A 库的表画像
    会跟着 B 库的估算一起回来,而两边都看不出这件事。
    """

    def __init__(self, profiles: dict | None = None) -> None:
        self._profiles = profiles or {}
        self.asked: list[tuple[str, str]] = []

    async def table_profile(self, datasource, table):
        self.asked.append((datasource, table))
        return self._profiles.get(table) or TableProfile(table=table)


def _profiles(**by_table) -> _CostProfiles:
    return _CostProfiles({
        name: TableProfile(table=name, **kw) for name, kw in by_table.items()
    })


def _explained_rows(rows: int, seen: list | None = None):
    """``connectors.explain`` 的替身:能报行数、也记下被问了什么。"""
    async def explain(sql, datasource=None):
        if seen is not None:
            seen.append(sql)
        return _qresult([(1, "ALL", "t", rows)],
                        columns=["id", "select_type", "table", "rows"])
    return explain


def _pricing_budget(rows: int | None = None, **over) -> BudgetService:
    """三档里选一档:``rows`` 给了就是 EXPLAIN 档,没给就是无依据档。"""
    if rows is None:
        return BudgetService(ExecutionBudget(max_rows=1000, **over))
    return BudgetService(
        ExecutionBudget(max_rows=1000, **over),
        explain=_explained_rows(rows), parse_explain=lambda d, r: rows,
    )


class TestDescribeCostShape:
    """§7.2 的 wire 形状就是接口契约:多一个字段、少一个字段都是改动。

    这个 dict 直接进模型的上下文,字段名是它能读到的全部信息 —— 形状漂移
    不会报错,只会让模型按旧名字取到一个 ``None``。
    """

    async def test_returns_exactly_the_documented_keys(self):
        out = await describe_cost(
            "SELECT name FROM students", budget=_pricing_budget(1_000),
            datasource="db", dialect="mysql",
        )
        assert set(out) == {
            "estimated_rows", "estimated_bytes", "source", "degraded",
            "verdict", "tables", "reason",
        }

    async def test_reports_the_estimate_and_the_verdict(self):
        out = await describe_cost(
            "SELECT name FROM students", budget=_pricing_budget(1_000),
            datasource="db", dialect="mysql",
        )
        assert out["estimated_rows"] == 1_000
        assert out["source"] == "explain"
        assert out["degraded"] is False
        assert out["verdict"] == "allow"

    async def test_the_tables_it_would_touch_carry_their_profile_numbers(self):
        """表清单从 SQL 解出来,数字从画像取 —— 两者**分别**可缺。"""
        profiles = _profiles(students={"row_count": 800, "bytes": 4096})
        out = await describe_cost(
            "SELECT s.name FROM students s JOIN grades g ON s.id = g.sid",
            budget=_pricing_budget(1_000), datasource="db", dialect="mysql",
            profiles=profiles,
        )
        assert out["tables"] == [
            {"name": "grades", "row_count": None, "bytes": None},
            {"name": "students", "row_count": 800, "bytes": 4096},
        ]
        assert profiles.asked == [("db", "grades"), ("db", "students")]

    async def test_a_table_nobody_knows_has_no_numbers_not_zeros(self):
        """查不到是 ``None``(§6.1「不可得 ≠ 0」)—— 0 在这里是个假依据。"""
        out = await describe_cost(
            "SELECT * FROM ghosts", budget=_pricing_budget(1_000),
            datasource="db", dialect="mysql", profiles=_profiles(),
        )
        assert out["tables"] == [{"name": "ghosts", "row_count": None, "bytes": None}]

    async def test_no_profile_service_still_names_the_tables(self):
        """画像没装配 ≠ 表清单也没有 —— 后者是从 SQL 解出来的,不依赖画像。"""
        out = await describe_cost(
            "SELECT * FROM students", budget=_pricing_budget(1_000),
            datasource="db", dialect="mysql",
        )
        assert out["tables"] == [{"name": "students", "row_count": None, "bytes": None}]

    async def test_unparseable_sql_yields_no_table_claim(self):
        """解不出表名 → 空清单,而不是把整条 SQL 当成表名报出去。"""
        out = await describe_cost(
            "SELECT FROM WHERE ((", budget=_pricing_budget(1_000), dialect="mysql",
        )
        assert out["tables"] == []


class TestDescribeCostRefusesWhatItCannotPrice:
    """问价工具的第一条职责是**只给能跑的 SQL 报价**。"""

    async def test_a_write_statement_is_refused_without_being_explained(self):
        """写语句不给报价:**不去 EXPLAIN 它**。

        EXPLAIN 本身要落库(它就是一条查询)。为一条注定被拒的写语句付一次
        EXPLAIN,等于把拒绝变成了额外开销。
        """
        seen: list[str] = []
        budget = BudgetService(
            ExecutionBudget(max_rows=1000),
            explain=_explained_rows(1, seen), parse_explain=lambda d, r: 1,
        )
        out = await describe_cost(
            "DELETE FROM students", budget=budget, dialect="mysql",
        )
        assert seen == []
        assert out["verdict"] == "reject"
        assert out["estimated_rows"] is None
        assert out["reason"]

    async def test_the_rejection_says_why_so_the_model_can_fix_it(self):
        out = await describe_cost(
            "UPDATE students SET grade = 0", budget=_pricing_budget(1_000),
            dialect="mysql",
        )
        assert out["degraded"] is False  # 不是「降级了」,是「没给报价」
        assert out["source"] == ""       # 也没有估算来源可言
        assert "只读" in out["reason"] or "read-only" in out["reason"]

    async def test_unparseable_sql_is_not_a_read_only_violation(self):
        """解析失败 fail-open(与执行路径同一条政策):方言盲区 ≠ 权限违规。

        真进去之后它会落在「无依据」那档,而不是被误报成写语句。
        """
        out = await describe_cost(
            "SELECT FROM WHERE ((", budget=_pricing_budget(1_000), dialect="mysql",
        )
        assert out["verdict"] != "reject"

    async def test_no_basis_is_degraded_and_shows_the_conservative_bytes(self):
        """无依据要**说出来**(I2),并把保守预算的字节数一并给出。"""
        budget = _pricing_budget(None, assume_max_scan_bytes=123, on_unestimable="degrade")
        out = await describe_cost("SELECT 1", budget=budget, dialect="mysql")
        assert out["source"] == "conservative"
        assert out["degraded"] is True
        assert out["verdict"] == "degrade"
        assert out["estimated_bytes"] == 123
        assert out["estimated_rows"] is None

    async def test_strict_deployments_report_reject_instead_of_degrade(self):
        """§8.3 C:``on_unestimable=reject`` 的部署里,无依据就是拒绝。"""
        budget = _pricing_budget(None, on_unestimable="reject")
        out = await describe_cost("SELECT 1", budget=budget, dialect="mysql")
        assert out["verdict"] == "reject"

    async def test_over_the_hard_cap_reports_reject_with_the_threshold(self):
        """顶到硬限 → 拒绝,且理由里带上阈值(模型要据此重写)。"""
        out = await describe_cost(
            "SELECT * FROM big", budget=_pricing_budget(5_000_000_000), dialect="mysql",
        )
        assert out["verdict"] == "reject"
        assert out["estimated_rows"] == 5_000_000_000
        assert "1000000000" in out["reason"] or "1_000_000_000" in out["reason"]
