"""判定侧块序列取数单测 —— 计划对齐 / 桶标签对齐 / 按维一条 SQL。

契约核心:``plan_blocks`` 的**硬对齐**(派生粒度必须等于生效粒度)与
``_block_index`` 的**前缀安全**桶匹配(月桶标签是 7 位,窗口边界是
10 位)。``fetch_block_series`` 用 FakeRunner 罐头行验证列契约。
"""

from __future__ import annotations

from trove.services.decision.series_source import (
    BlockSeries,
    fetch_block_series,
    plan_blocks,
    plan_reason,
)
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
    """记录 SQL,回放罐头行。"""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls: list[str] = []

    async def __call__(self, sql: str, ds: str):
        self.calls.append(sql)
        return ["region", "bucket", "balance"], self.rows


async def _fetch(rows, **overrides):
    kwargs = dict(
        semantic_layer=_SL(), runner=_Runner(rows), datasource="demo",
        dialect="sqlite", metric_name="balance", matched=["loan"],
        filters=[{"field": "loan.region", "op": "=", "value": "华东"}],
        window=MONTH, time_field="loan.day", lookback=3,
        dimensions=["region"],
    )
    kwargs.update(overrides)
    runner = kwargs["runner"]
    series = await fetch_block_series(**kwargs)
    return series, runner


class TestPlanBlocks:
    def test_month_window_derives_month_grain(self):
        eff, mode, blocks = plan_blocks(MONTH, lookback=2)
        assert eff == "month" and mode == "trailing"
        assert blocks == [("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-31")]

    def test_declared_grain_must_equal_the_derived_one(self):
        assert plan_blocks(MONTH, grain="day", lookback=2) is None
        assert plan_blocks(MONTH, grain="month", lookback=2) is not None

    def test_unaligned_window_refuses(self):
        assert plan_blocks(("2026-09-01", "2026-09-15"), lookback=2) is None
        assert plan_blocks(None, lookback=2) is None

    def test_same_phase_mode(self):
        eff, mode, blocks = plan_blocks(MONTH, mode="same_phase", lookback=2)
        assert mode == "same_phase"
        assert blocks == [("2024-09-01", "2024-09-30"), ("2025-09-01", "2025-09-30")]


class TestPlanReason:
    def test_empty_reason_when_plannable(self):
        assert plan_reason(MONTH, lookback=3) == ""

    def test_vocabulary_disambiguates_the_failure(self):
        assert plan_reason(None) == "no_window"
        assert plan_reason(("2026-09-01", "2026-09-15")) == "grain_unaligned"
        assert plan_reason(MONTH, grain="week") == "grain_mismatch"
        assert plan_reason(MONTH, lookback=0) == "no_blocks"

    def test_agrees_with_plan_blocks_on_every_case(self):
        # 原因判定与主路径必须同进同出,否则证据里的 reason 会撒谎。
        cases = [
            dict(window=None),
            dict(window=("2026-09-01", "2026-09-15")),
            dict(window=MONTH, grain="week"),
            dict(window=MONTH, lookback=0),
            dict(window=MONTH, lookback=3),
        ]
        for c in cases:
            window = c.pop("window")
            reason = plan_reason(window, **c)
            planned = plan_blocks(window, **c)
            assert (reason == "") is (planned is not None)


class TestFetchBlockSeries:
    async def test_parses_buckets_into_blocks_per_dim(self):
        rows = [
            ["华东", "2026-07", 100.0],
            ["华东", "2026-08", 110.0],
            ["华北", "2026-08", 50.0],
        ]
        series, runner = await _fetch(rows)
        assert series is not None
        assert len(runner.calls) == 1                       # 按维仍是一条 SQL
        assert series.grain == "month" and series.mode == "trailing"
        # lookback=3 → 三个严格早于 2026-09 的月块
        assert series.blocks == [("2026-06-01", "2026-06-30"),
                                 ("2026-07-01", "2026-07-31"),
                                 ("2026-08-01", "2026-08-31")]
        assert series.values("华东") == [None, 100.0, 110.0]   # 缺块占位为 None
        assert series.values("华北") == [None, None, 50.0]
        assert series.values("不存在") == []
        assert series.unmatched == 0

    async def test_sql_groups_by_dimension_and_bucket(self):
        _, runner = await _fetch([])
        sql = runner.calls[0]
        assert "loan.region" in sql and "strftime('%Y-%m'" in sql
        assert "loan.region" in sql.split("GROUP BY")[-1]

    async def test_unmatched_buckets_are_counted_not_dropped(self):
        rows = [["华东", "2025-01", 1.0], ["华东", "2026-08", 2.0]]
        series, _ = await _fetch(rows)
        assert series.unmatched == 1
        assert series.values("华东") == [None, None, 2.0]

    async def test_short_rows_are_skipped(self):
        rows = [["华东", "2026-08"], ["华东", "2026-08", 2.0]]
        series, _ = await _fetch(rows)
        assert series.values("华东") == [None, None, 2.0]

    async def test_aggregate_rule_labels_every_bucket_as_the_empty_dim(self):
        rows = [["2026-07", 5.0], ["2026-08", 6.0]]
        series, _ = await _fetch(rows, dimensions=[], filters=[])
        assert series.values("") == [None, 5.0, 6.0]

    async def test_soft_miss_returns_none_without_executing(self):
        series, runner = await _fetch([], metric_name="nope")
        assert series is None and runner.calls == []

    async def test_unaligned_window_returns_none_without_executing(self):
        series, runner = await _fetch([], window=("2026-09-01", "2026-09-15"))
        assert series is None and runner.calls == []

    async def test_runner_exception_propagates(self):
        class _Boom:
            async def __call__(self, sql, ds):
                raise RuntimeError("boom")

        try:
            await _fetch([], runner=_Boom())
        except RuntimeError as e:
            assert "boom" in str(e)
        else:  # pragma: no cover
            raise AssertionError("exception must propagate to the caller")


class TestIncludeCurrent:
    """因果梯的取数契约:当期窗口本体是序列的最后一个块。

    缺省 ``include_current=False`` 时逐字节不变(显著性路径依赖这一点)。
    """

    async def test_current_block_rides_last(self):
        rows = [["华东", "2026-08", 110.0], ["华东", "2026-09", 200.0]]
        series, _ = await _fetch(rows, include_current=True)
        assert series.blocks == [("2026-06-01", "2026-06-30"),
                                 ("2026-07-01", "2026-07-31"),
                                 ("2026-08-01", "2026-08-31"),
                                 ("2026-09-01", "2026-09-30")]
        # 末位 = 当期(因果侧 split_post 的约定)
        assert series.values("华东") == [None, None, 110.0, 200.0]

    async def test_absence_keeps_blocks_without_the_window(self):
        rows = [["华东", "2026-09", 200.0]]
        series, _ = await _fetch(rows)
        assert len(series.blocks) == 3 and series.unmatched == 1


class TestBlockSeriesHelpers:
    def test_span_is_first_start_to_last_end(self):
        s = BlockSeries(sql="s", grain="month", mode="trailing",
                        blocks=[("2026-07-01", "2026-07-31"),
                                ("2026-08-01", "2026-08-31")])
        assert s.span() == ("2026-07-01", "2026-08-31")

    def test_empty_blocks_span_is_none(self):
        assert BlockSeries(sql="s", grain="month", mode="trailing").span() is None
