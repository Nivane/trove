"""因果梯取数单测 —— 过滤手术(纯)、窗口放宽、处理组/对照帧列契约。

过滤手术是因果梯最容易出错的一环:摘多了(把口径过滤当身份过滤摘掉)
供体池被污染,摘少了(钉子没识别出来)处理组 = 整个总体、对照组
一行都不剩 —— 两种错误都只在证据里显形。FakeRunner 验证帧的
列契约与"当期块确实被取到"。
"""

from __future__ import annotations

import pytest

from trove.services.decision.causal_source import (
    causal_lookback,
    donor_plan,
    fetch_control_series,
    fetch_treated_series,
    override_filters,
    split_post,
)
from trove.services.decision.rules import Causal, CausalControl, Seasonal
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
)

MONTH = ("2026-09-01", "2026-09-30")


def _model() -> SemanticModel:
    return SemanticModel(
        name="m",
        datasets=[SemanticDataset(
            name="loan", source="loan",
            fields=[
                SemanticField(name="amount", expression="loan.amount", semantic_role="measure"),
                SemanticField(name="region", expression="loan.region", semantic_role="dimension"),
                SemanticField(name="day", expression="loan.day", datatype="Date", is_time=True),
            ],
        )],
        metrics=[SemanticMetric(name="balance", expression="SUM(loan.amount)", datasets=["loan"])],
    )


class _SL:
    def model(self):
        return _model()


class _Runner:
    """记录 SQL,回放罐头行(列数由调用方按 dimensions 对齐)。"""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls: list[str] = []

    async def __call__(self, sql: str, ds: str):
        self.calls.append(sql)
        return ["c0", "c1", "c2"], self.rows


# ── 纯函数 ────────────────────────────────────────────────


class TestCausalLookback:
    def test_seasonal_lookback_wins_when_largest(self):
        assert causal_lookback(Seasonal(lookback=12), Causal(placebo_blocks=4)) == 12

    def test_placebo_blocks_plus_one(self):
        # 10 对相邻块对需要 11 个块 —— 声明了就得给够
        assert causal_lookback(Seasonal(lookback=2), Causal(placebo_blocks=10)) == 11

    def test_min_blocks_floor(self):
        # C1 稳定基线是硬门,取数时就按它兜底,免得块不够白跑 SQL
        assert causal_lookback(Seasonal(lookback=1), Causal(placebo_blocks=1)) == 8

    def test_none_lookback_is_tolerated(self):
        assert causal_lookback(Seasonal(lookback=None), Causal(placebo_blocks=4)) == 8


class TestDonorPlan:
    def test_eq_pin_becomes_treated_label(self):
        donors, treated = donor_plan(
            CausalControl(dim="region", value="华北"),
            [{"field": "loan.region", "op": "=", "value": "华东"}])
        assert treated == ["华东"] and donors == []

    def test_double_eq_and_default_op_are_both_pins(self):
        ctl = CausalControl(dim="region", value="华北")
        assert donor_plan(ctl, [{"field": "region", "op": "==", "value": "华东"}])[1] == ["华东"]
        assert donor_plan(ctl, [{"field": "region", "value": "华东"}])[1] == ["华东"]

    def test_in_list_yields_each_label(self):
        _, treated = donor_plan(
            CausalControl(dim="region", value="华北"),
            [{"field": "region", "op": "in", "value": ["华东", "华南"]}])
        assert treated == ["华东", "华南"]

    def test_no_pin_means_treated_unidentified(self):
        donors, treated = donor_plan(
            CausalControl(dim="region", value="华北"),
            [{"field": "product", "op": "=", "value": "车贷"}])
        assert treated is None
        assert donors == [{"field": "product", "op": "=", "value": "车贷"}]

    def test_cross_convention_bare_vs_qualified(self):
        # 编译器两种写法都收(loan.region / region);声明侧写语义名,
        # 识别必须跨约定成立(否则供体池悄悄混进处理组)
        _, treated = donor_plan(
            CausalControl(dim="region", value="华北"),
            [{"field": "loan.region", "op": "=", "value": "华东"}])
        assert treated == ["华东"]
        _, treated = donor_plan(
            CausalControl(dim="loan.region", value="华北"),
            [{"field": "region", "op": "=", "value": "华东"}])
        assert treated == ["华东"]

    def test_non_equality_filters_survive_on_both_sides(self):
        donors, treated = donor_plan(
            CausalControl(dim="region", value="华北"),
            [{"field": "region", "op": "=", "value": "华东"},
             {"field": "loan.amount", "op": ">=", "value": 100}])
        assert treated == ["华东"]
        assert donors == [{"field": "loan.amount", "op": ">=", "value": 100}]

    def test_none_subject_filters(self):
        assert donor_plan(CausalControl(dim="region", value="华北"), None) == ([], None)

    def test_does_not_mutate_input(self):
        src = [{"field": "region", "op": "=", "value": "华东"}]
        donor_plan(CausalControl(dim="region", value="华北"), src)
        assert src == [{"field": "region", "op": "=", "value": "华东"}]


class TestOverrideFilters:
    def test_control_eq_overrides_subject_pin_on_same_field(self):
        # 直拼会得到 region=华东 AND region=华北 → 空集
        out = override_filters(
            [{"field": "loan.region", "op": "=", "value": "华东"},
             {"field": "product", "op": "=", "value": "车贷"}],
            [{"field": "loan.region", "op": "=", "value": "华北"}])
        assert out == [{"field": "product", "op": "=", "value": "车贷"},
                       {"field": "loan.region", "op": "=", "value": "华北"}]

    def test_untouched_subject_filters_stay(self):
        out = override_filters(
            [{"field": "region", "op": "=", "value": "华东"}],
            [{"field": "product", "op": "=", "value": "房贷"}])
        assert out == [{"field": "region", "op": "=", "value": "华东"},
                       {"field": "product", "op": "=", "value": "房贷"}]

    def test_non_eq_control_is_additional_not_override(self):
        out = override_filters(
            [{"field": "region", "op": "=", "value": "华东"}],
            [{"field": "loan.amount", "op": ">=", "value": 100}])
        assert {"field": "region", "op": "=", "value": "华东"} in out
        assert len(out) == 2

    def test_empty_sides(self):
        assert override_filters(None, None) == []
        assert override_filters(None, [{"field": "r", "op": "=", "value": "x"}]) == \
            [{"field": "r", "op": "=", "value": "x"}]


class TestSplitPost:
    def test_last_is_current(self):
        assert split_post([1, 2, 3]) == ([1, 2], 3)

    def test_empty_and_single(self):
        assert split_post([]) == ([], None)
        assert split_post(None) == ([], None)
        assert split_post([7]) == ([], 7)


# ── 取数(至多 2 条 SQL 的帧)──────────────────────────────


def _treated_kwargs(**overrides):
    kwargs = dict(
        semantic_layer=_SL(), datasource="demo", dialect="sqlite",
        metric_name="balance", matched=["loan"],
        filters=[{"field": "loan.region", "op": "=", "value": "华东"}],
        window=MONTH, time_field="loan.day", lookback=3,
    )
    kwargs.update(overrides)
    return kwargs


class TestFetchTreatedSeries:
    async def test_current_block_is_appended_and_parsed(self):
        rows = [["2026-06", 100.0], ["2026-07", 110.0],
                ["2026-08", 120.0], ["2026-09", 200.0]]
        runner = _Runner(rows)
        series = await fetch_treated_series(runner=runner, **_treated_kwargs())
        assert series is not None and len(runner.calls) == 1
        assert series.blocks == [("2026-06-01", "2026-06-30"),
                                 ("2026-07-01", "2026-07-31"),
                                 ("2026-08-01", "2026-08-31"),
                                 ("2026-09-01", "2026-09-30")]
        assert series.values("") == [100.0, 110.0, 120.0, 200.0]

    async def test_fetch_span_reaches_through_the_current_window(self):
        runner = _Runner([])
        await fetch_treated_series(runner=runner, **_treated_kwargs())
        sql = runner.calls[0]
        # 半开区间:end + 1 天 —— 当期 9-30 整天在取数范围内
        assert "2026-06-01" in sql and "2026-10-01" in sql

    async def test_soft_miss_executes_nothing(self):
        runner = _Runner([])
        series = await fetch_treated_series(
            runner=runner, **_treated_kwargs(metric_name="nope"))
        assert series is None and runner.calls == []


def _control_kwargs(**overrides):
    kwargs = dict(
        semantic_layer=_SL(), datasource="demo", dialect="sqlite",
        metric_name="balance", matched=["loan"],
        subject_filters=[{"field": "loan.region", "op": "=", "value": "华东"}],
        control=CausalControl(dim="region", value="华北"),
        window=MONTH, time_field="loan.day", lookback=3,
    )
    kwargs.update(overrides)
    return kwargs


class TestFetchControlSeriesDimMode:
    ROWS = [["华东", "2026-08", 120.0], ["华东", "2026-09", 200.0],
            ["华北", "2026-08", 60.0], ["华北", "2026-09", 65.0],
            ["华南", "2026-08", 30.0], ["华南", "2026-09", 50.0],
            ["西南", "2026-09", 80.0]]

    async def test_labels_and_donor_exclusion(self):
        runner = _Runner(self.ROWS)
        frame = await fetch_control_series(runner=runner, **_control_kwargs())
        assert frame.mode == "dim" and frame.label == "华北"
        assert frame.treated_labels == ["华东"]
        # 供体 = 其余维值;处理组与对照组都不进池
        assert sorted(frame.donors) == ["华南", "西南"]
        assert frame.donor_reason == ""

    async def test_subject_pin_is_surgically_removed_from_the_sql(self):
        runner = _Runner(self.ROWS)
        await fetch_control_series(runner=runner, **_control_kwargs())
        sql = runner.calls[0]
        assert "华东" not in sql                      # 钉子摘掉,分组才有对照
        assert "loan.region" in sql.split("GROUP BY")[-1]

    async def test_treated_unidentified_when_subject_has_no_pin(self):
        runner = _Runner(self.ROWS)
        # subject 只在非处理维上有口径(不是等值钉)→ 处理组 = 整个总体,
        # 任何供体都是它的子集 → 合成对照被污染,标因而非硬凑
        frame = await fetch_control_series(
            runner=runner,
            **_control_kwargs(subject_filters=[
                {"field": "loan.amount", "op": ">=", "value": 100}]))
        assert frame.treated_labels is None
        assert frame.donors == {}
        assert frame.donor_reason == "treated_unidentified"

    async def test_no_donors_when_only_treated_and_control_rows_exist(self):
        runner = _Runner([["华东", "2026-09", 200.0], ["华北", "2026-09", 65.0]])
        frame = await fetch_control_series(runner=runner, **_control_kwargs())
        assert frame.donors == {} and frame.donor_reason == "no_donors"

    async def test_soft_miss_keeps_frame_empty(self):
        runner = _Runner([])
        frame = await fetch_control_series(
            runner=runner, **_control_kwargs(metric_name="nope"))
        assert frame.series is None and frame.donors == {}
        assert frame.mode == "dim" and frame.label == "华北"


class TestFetchControlSeriesFiltersMode:
    async def test_control_filters_override_the_subject_pin(self):
        runner = _Runner([["2026-09", 65.0]])
        frame = await fetch_control_series(
            runner=runner,
            **_control_kwargs(control=CausalControl(
                filters=[{"field": "loan.region", "op": "=", "value": "华北"}])))
        sql = runner.calls[0]
        assert frame.mode == "filters"
        assert "华北" in sql and "华东" not in sql
        # 供体池结构上不存在 → L3 不可用,诚实标因
        assert frame.donor_reason == "donors_need_dim_control"
        assert frame.donors == {}

    async def test_series_parses_as_aggregate_frame(self):
        runner = _Runner([["2026-08", 60.0], ["2026-09", 65.0]])
        frame = await fetch_control_series(
            runner=runner,
            **_control_kwargs(control=CausalControl(
                filters=[{"field": "loan.region", "op": "=", "value": "华北"}])))
        # lookback=3 → 三个历史块(6/7/8 月)+ 当期(9 月)
        assert frame.series.values("") == [None, None, 60.0, 65.0]


def test_frames_never_write_back_to_subject_filters():
    """取数帧是只读消费:subject 过滤在两个模式下都不被改写。"""
    filters = [{"field": "loan.region", "op": "=", "value": "华东"}]
    donor_plan(CausalControl(dim="region", value="华北"), filters)
    override_filters(filters, [{"field": "loan.region", "op": "=", "value": "华北"}])
    assert filters == [{"field": "loan.region", "op": "=", "value": "华东"}]


@pytest.mark.parametrize("bad", [None, "", []])
def test_donor_plan_tolerates_empty_subject_filters(bad):
    donors, treated = donor_plan(CausalControl(dim="region", value="华北"), bad)
    assert donors == [] and treated is None
