"""分析→决策桥单测(记录型 fake runner:零 I/O、零 LLM、零网络)。

钉死补丁 1 的三条纪律:

  - **预算硬上限** ≤ 4 条查询:维度路径 = 分组 2 + 树 2;复用路径 = hop0 2
    + 树 2;两条路径都不许越界,查询数由 fake runner 数出来;
  - **不适用 ≠ 失败**:单叶子度量 / 度量不可解析 → ``None`` 且**零查询**,
    证据里什么都不写;
  - **该做没做成 → degraded**:runner 报错只记账,已拿到的部分照常交付。
"""

from __future__ import annotations

from typing import Any

from trove.services.decision.bridge import (
    MAX_BRIDGE_QUERIES,
    primary_driver_line,
    run_bridge,
)
from trove.services.decision.rules import parse_rule
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
)

# ── 罐头数字(单日窗口,当前期用起始字面量 >= '2024-02-01' 区分)──
# profit = revenue − expense,加性恒等式:Δ70−60 = (100−80) − (30−20) ✓
PROFIT_C, PROFIT_B = 70.0, 60.0
REV_C, REV_B = 100.0, 80.0
EXP_C, EXP_B = 30.0, 20.0
# 维度分组自洽:East 55+West 15 = 70;East 35+West 25 = 60
DIM_C = [["East", 55.0], ["West", 15.0]]
DIM_B = [["East", 35.0], ["West", 25.0]]

CUR = ("2024-02-01", "2024-02-01")
BASE = ("2024-01-31", "2024-01-31")


def _model() -> SemanticModel:
    return SemanticModel(
        name="m",
        datasets=[SemanticDataset(
            name="sales", source="sales",
            fields=[
                SemanticField(name="amount", expression="sales.amount",
                              semantic_role="measure"),
                SemanticField(name="cost", expression="sales.cost",
                              semantic_role="measure"),
                SemanticField(name="region", expression="sales.region",
                              semantic_role="dimension"),
                SemanticField(name="channel", expression="sales.channel",
                              semantic_role="dimension"),
                SemanticField(name="day", expression="sales.day",
                              datatype="Date", is_time=True),
            ],
        )],
        metrics=[
            SemanticMetric(name="revenue", expression="SUM(sales.amount)",
                           datasets=["sales"]),
            SemanticMetric(name="expense", expression="SUM(sales.cost)",
                           datasets=["sales"]),
            SemanticMetric(name="profit", expression="revenue - expense",
                           datasets=["sales"], metric_type="derived"),
            # 单叶子:桥对这条度量没有话说(不是失败)
            SemanticMetric(name="avg_amount", expression="AVG(sales.amount)",
                           datasets=["sales"]),
        ],
    )


class _SL:
    """最小语义层桩:引擎与桥只用 ``.model()``。"""

    def __init__(self, model: SemanticModel | None = None) -> None:
        self._model = model or _model()

    def model(self) -> SemanticModel:
        return self._model


def _n_select_cols(sql: str) -> int:
    head = sql.split("FROM", 1)[0]
    inner = head.split("SELECT", 1)[1]
    return len([p for p in inner.split(",") if p.strip()])


class FakeRunner:
    """记录型 runner:按 SQL 形状回罐头行。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def __call__(self, sql: str, datasource: str
                       ) -> tuple[list[str], list[list[Any]]]:
        self.calls.append(sql)
        if self.fail:
            raise RuntimeError("datasource down")
        # 半开区间:基期 SQL 也含 '< '2024-02-01'',只有 >= 一侧能区分两期
        cur = ">= '2024-02-01'" in sql
        if "GROUP BY" in sql.upper():
            return ["region", "profit"], [list(r) for r in (DIM_C if cur else DIM_B)]
        if _n_select_cols(sql) >= 3:      # 树组合查询 profit/revenue/expense
            return (["profit", "revenue", "expense"],
                    [[PROFIT_C, REV_C, EXP_C]] if cur
                    else [[PROFIT_B, REV_B, EXP_B]])
        return ["profit"], [[PROFIT_C if cur else PROFIT_B]]


def rule(**overrides):
    return parse_rule({
        "id": "profit-drop", "name": "利润下滑", "window": "本月",
        "subject": {"metrics": ["profit"], "dimensions": ["region"]},
        "baseline": {"kind": "prev_period"},
        "scope": "per_dimension",
        "conditions": ["delta < 0"],
        **overrides,
    })


async def bridge(runner: FakeRunner, rule_obj=None, *, judged_rows=None,
                 semantic_layer=None):
    return await run_bridge(
        semantic_layer=semantic_layer or _SL(), runner=runner,
        rule=rule_obj or rule(), datasource="demo", dialect="sqlite",
        cur_period=CUR, base_period=BASE, judged_rows=judged_rows,
    )


class TestBudget:
    """整座桥 ≤ 4 条查询 —— 由 runner 数出来,不靠注释保证。"""

    async def test_dimension_path_costs_four_queries(self):
        runner = FakeRunner()
        out = await bridge(runner, rule(
            scope="aggregate", subject={"metrics": ["profit"]},
            driver_dimension="region"))
        assert len(runner.calls) == MAX_BRIDGE_QUERIES
        assert sum("GROUP BY" in s.upper() for s in runner.calls) == 2  # 分组两条
        assert out["top_components"][0]["source"] == "dimension"

    async def test_reused_path_costs_no_dimension_query(self):
        """单维 per_dimension:判定行本身就是该维的分组值,零额外查询。"""
        runner = FakeRunner()
        out = await bridge(runner, rule(), judged_rows=[
            {"dim": "East", "delta": -20.0, "contribution": -1.0},
            {"dim": "West", "delta": 5.0, "contribution": 0.25},
        ])
        assert len(runner.calls) == MAX_BRIDGE_QUERIES
        assert not any("GROUP BY" in s.upper() for s in runner.calls)
        assert out["top_components"][0]["dim"] == "region"     # 语义仍是"按 region"
        assert out["top_components"][0]["value"] == "East"
        assert out["top_components"][0]["source"] == "dimension"

    async def test_tree_only_when_no_single_dimension(self):
        """多维标签("region / channel")拆不回单维 → 宁可不给,不给错的。"""
        runner = FakeRunner()
        out = await bridge(runner, rule(
            subject={"metrics": ["profit"],
                     "dimensions": ["region", "channel"]}))
        assert len(runner.calls) == MAX_BRIDGE_QUERIES
        assert out["top_components"], "树还有话说,不该整桥退出"
        assert all(c["source"] == "tree" for c in out["top_components"])

    async def test_not_applicable_metric_is_free(self):
        """单叶子度量:桥不适用,**零查询**(判定是常态,分析是例外)。"""
        runner = FakeRunner()
        out = await bridge(runner, rule(
            subject={"metrics": ["revenue"], "dimensions": ["region"]}))
        assert out is None
        assert runner.calls == []

    async def test_unresolvable_metric_is_free(self):
        runner = FakeRunner()
        out = await bridge(runner, rule(
            subject={"metrics": ["nope"], "dimensions": ["region"]}))
        assert out is None
        assert runner.calls == []

    async def test_queries_are_recorded_for_replay(self):
        """证据不认"谁跑的":引擎跑的两条与桥自己跑的两条,都要在 queries 里。"""
        runner = FakeRunner()
        out = await bridge(runner, rule(
            scope="aggregate", subject={"metrics": ["profit"]},
            driver_dimension="region"))
        assert len(out["queries"]) == MAX_BRIDGE_QUERIES
        assert [q["id"] for q in out["queries"]] == [1, 2, 3, 4]
        purposes = [q["purpose"] for q in out["queries"]]
        assert purposes.count("driver_dimension") == 2
        assert all(q.get("sql") for q in out["queries"])
        assert {q.get("period") for q in out["queries"]} == {"current", "base"}


class TestValues:
    async def test_dimension_components_carry_delta_and_contribution(self):
        out = await bridge(FakeRunner(), rule(
            scope="aggregate", subject={"metrics": ["profit"]},
            driver_dimension="region"))
        east, west = out["top_components"]
        assert (east["dim"], east["value"]) == ("region", "East")
        assert east["delta"] == 20.0 and west["delta"] == -10.0
        # 贡献 = Δ / Σ|Δ| = 20/30、-10/30 —— 与判定行同一口径
        assert round(east["contribution"], 4) == round(20 / 30, 4)
        assert round(west["contribution"], 4) == round(-10 / 30, 4)

    async def test_top_components_sorted_by_absolute_delta(self):
        out = await bridge(FakeRunner(), rule(
            scope="aggregate", subject={"metrics": ["profit"]},
            driver_dimension="region"))
        assert [c["value"] for c in out["top_components"]] == ["East", "West"]

    async def test_residual_comes_from_the_tree_root(self):
        """加性恒等式:Δ(70−60) == Δrevenue − Δexpense,残差精确。"""
        out = await bridge(FakeRunner(), rule(
            subject={"metrics": ["profit"],
                     "dimensions": ["region", "channel"]}))
        assert out["residual"]["exact"] is True
        assert out["residual"]["value"] == 0.0
        assert out["residual"]["reason"] == "identity"

    async def test_tree_root_values_track_the_two_periods(self):
        out = await bridge(FakeRunner(), rule(
            subject={"metrics": ["profit"],
                     "dimensions": ["region", "channel"]}))
        root = out["tree"]
        assert root["current"] == PROFIT_C and root["base"] == PROFIT_B
        assert root["delta"] == PROFIT_C - PROFIT_B


class TestDegradation:
    async def test_runner_failure_degrades_but_still_returns(self):
        """桥坏了 ≠ 判定坏了:值拿不到的部分记账,判定照常交付。"""
        out = await bridge(FakeRunner(fail=True), rule(
            scope="aggregate", subject={"metrics": ["profit"]},
            driver_dimension="region"))
        assert out is not None
        assert out["degraded"], "失败必须响亮"
        assert out["top_components"] == []

    async def test_degraded_entries_carry_the_failing_sql(self):
        out = await bridge(FakeRunner(fail=True), rule(
            scope="aggregate", subject={"metrics": ["profit"]},
            driver_dimension="region"))
        group = [d for d in out["degraded"] if "group_failed" in d.get("reason", "")]
        assert group and all(d.get("sql") for d in group)

    async def test_bridge_crash_never_raises(self):
        """防御层:run_bridge 的最外层 try 是最后的保险 —— 判定已经判完,
        桥的任何形态的失败都不许上抛。"""
        class ExplodingRule:
            id = "boom"

            @property
            def subject(self):
                raise RuntimeError("boom")

        out = await bridge(FakeRunner(), ExplodingRule())
        assert out is not None
        assert out["degraded"] and out["top_components"] == []


class TestPrimaryDriverLine:
    def test_dimension_source_formats_as_dim_equals_value(self):
        line = primary_driver_line({"top_components": [
            {"dim": "region", "value": "East", "contribution": 0.62,
             "source": "dimension"}]})
        assert line == "region=East（贡献 62.0%）"

    def test_tree_source_formats_the_delta(self):
        line = primary_driver_line({"top_components": [
            {"dim": "expense", "value": -1234.0, "source": "tree"}]})
        assert line == "expense=-1,234"

    def test_without_a_contribution_the_label_stands_alone(self):
        line = primary_driver_line({"top_components": [
            {"dim": "region", "value": "East", "contribution": None,
             "source": "dimension"}]})
        assert line == "region=East"

    def test_zero_denominator_contribution_is_none(self):
        """Σ|Δ| = 0(全组没动)→ contribution None,不是 0 —— "没有波动"
        与"算不出占比"是两件事。"""
        from trove.services.decision.bridge import _contribution

        items = [{"delta": 0.0}, {"delta": 0.0}]
        _contribution(items)
        assert items[0]["contribution"] is None

    def test_nothing_to_say_is_empty(self):
        assert primary_driver_line(None) == ""
        assert primary_driver_line({}) == ""
        assert primary_driver_line({"top_components": []}) == ""
