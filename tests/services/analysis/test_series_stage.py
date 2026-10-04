"""块序列阶段(引擎集成):默认关逐字节兼容;开启时 1 条查询 + band。

新阶段纪律:
  - 默认关(``series_grain`` 空 ∧ ``request.series`` None)时跳序、查询数、
    payload 与老路径完全一致;
  - 开启后只发 **1 条** ``time_grain`` 查询;粒度不齐/空序列/预算让路
    一律进 ``degraded``(序列是增强不是前提,主产物照常交付);
  - ``total_query_budget`` 显式设置才进 evidence.budget(None = 无上限,
    只记账、不改行为)。
"""

from __future__ import annotations

from typing import Any

import pytest

from trove.services.analysis.engine import (
    AnalysisEngine,
    AnalysisLimits,
    AnalysisRequest,
    analysis_payload,
)
from trove.services.analysis.series import SeriesSpec
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
)

# 罐头数字与 test_engine 同构且自洽(net 总量 == 区域之和)
CUR, BASE = 70.0, 60.0
REV_C, REV_B = 100.0, 80.0
EXP_C, EXP_B = 30.0, 20.0
REGION_C, REGION_B = [["East", 45.0], ["West", 25.0]], [["East", 36.0], ["West", 24.0]]
SERIES_ROWS = [["2023-11", 40.0], ["2023-12", 42.0], ["2024-01", 44.0]]


def _model() -> SemanticModel:
    return SemanticModel(
        name="m",
        datasets=[SemanticDataset(
            name="sales", source="sales",
            fields=[
                SemanticField(name="amount", expression="sales.amount", semantic_role="measure"),
                SemanticField(name="cost", expression="sales.cost", semantic_role="measure"),
                SemanticField(name="region", expression="sales.region", semantic_role="dimension"),
                SemanticField(name="day", expression="sales.day", datatype="Date", is_time=True),
            ],
        )],
        metrics=[
            SemanticMetric(name="revenue", expression="SUM(sales.amount)", datasets=["sales"]),
            SemanticMetric(name="expense", expression="SUM(sales.cost)", datasets=["sales"]),
            SemanticMetric(name="net", expression="revenue - expense", datasets=["sales"],
                           metric_type="derived"),
        ],
    )


class _SL:
    def __init__(self, model: SemanticModel | None = None) -> None:
        self._model = model if model is not None else _model()

    def model(self) -> SemanticModel:
        return self._model


def _n_select_cols(sql: str) -> int:
    head = sql.split("FROM", 1)[0]
    inner = head.split("SELECT", 1)[1]
    return len([p for p in inner.split(",") if p.strip()])


def _is_series_sql(sql: str) -> bool:
    upper = sql.upper()
    return ("GROUP BY" in upper and "SALES.REGION" not in upper
            and "__NUM" not in upper and "STRFTIME" in upper)


class FakeRunner:
    """记录型 runner;序列查询按形状识别(唯一带 strftime 分组的查询)。"""

    def __init__(self, series_rows: list[list[Any]] | None = None) -> None:
        self.calls: list[str] = []
        self.series_rows = SERIES_ROWS if series_rows is None else series_rows

    async def __call__(self, sql: str, datasource: str) -> tuple[list[str], list[list[Any]]]:
        self.calls.append(sql)
        if _is_series_sql(sql):
            return ["bucket", "net"], [list(r) for r in self.series_rows]
        cur = ">= '2024-02-01'" in sql
        if "GROUP BY sales.region" in sql:
            return ["region", "net"], [list(r) for r in (REGION_C if cur else REGION_B)]
        n = _n_select_cols(sql)
        if n >= 3:
            return (["net", "revenue", "expense"],
                    [[CUR, REV_C, EXP_C]] if cur else [[BASE, REV_B, EXP_B]])
        if " - " in sql:
            return ["net"], [[CUR if cur else BASE]]
        return ["revenue"], [[REV_C if cur else REV_B]]


def _req(**kw: Any) -> AnalysisRequest:
    base: dict[str, Any] = {
        "question": "net 这个月为什么变化?",
        "lang": "zh",
        "datasource": "demo",
        "dialect": "sqlite",
        "matched": ["sales"],
        "metric": "net",
        "dimensions": ["region"],
        "time_context": "2024-02-01 ~ 2024-02-01",
    }
    base.update(kw)
    return AnalysisRequest(**base)


def _engine(runner: FakeRunner, **limits: Any) -> AnalysisEngine:
    # driver_tree=False:序列阶段的用例只查序列本身,树另有用例
    return AnalysisEngine(_SL(), runner, AnalysisLimits(driver_tree=False, **limits))


class TestDefaultOff:
    async def test_byte_compatible_when_off(self):
        runner = FakeRunner()
        out = await _engine(runner).run(_req())
        assert out is not None
        purposes = [q["purpose"] for q in out.evidence_queries]
        assert purposes == ["overall", "overall", "probe", "probe"]
        assert len(runner.calls) == 4
        assert out.series is None and out.budget is None
        p = analysis_payload(out, question="q", chart=None,
                             baseline_label="", datasource="demo")
        assert "series" not in p
        assert "budget" not in p["evidence"]
        assert out.degraded == []


class TestSeriesEnabled:
    async def test_request_spec_one_query_and_band(self):
        runner = FakeRunner()
        out = await _engine(runner).run(
            _req(series=SeriesSpec(grain="month", lookback=3, mode="trailing")))
        assert out is not None
        # 只发 1 条序列查询,插在树之前(kind=driver_tree 关)
        purposes = [q["purpose"] for q in out.evidence_queries]
        assert purposes == ["overall", "overall", "probe", "probe", "series"]
        assert len(runner.calls) == 5
        s = out.series
        assert s is not None
        assert s["grain"] == "month" and s["mode"] == "trailing" and s["lookback"] == 3
        assert s["span"] == ["2023-11-01", "2024-01-31"]
        assert s["labels"] == ["2023-11", "2023-12", "2024-01"]
        assert s["values"] == [40.0, 42.0, 44.0]
        # band:center=42;当前值 70 → z 远超阈值 → outside;块数 3 强制 low_n
        assert s["band"]["center"] == 42.0
        assert s["band"]["method"] == "robust"
        assert "insufficient_n" in s["band"]["degraded"]
        assert s["current"] == CUR and s["z"] == pytest.approx((70.0 - 42.0) / (1.4826 * 2.0))
        assert s["outside"] is True and s["low_n"] is True
        # 带宽与位置分数随序列走(B8):引擎是唯一产地,消费面(markdown /
        # 分析卡)不重算 —— 公式与判定侧 gate 同一份 confidence_from_margin
        assert s["k"] == 3.5
        assert s["confidence"] == 1.0          # |z|≈9.44 → (|z|−k)/k > 1 → 截断
        # 序列 SQL 覆盖历史 span 且带分桶;span 末日在 SQL 里是半开
        # (``< '2024-02-01'``)—— payload 的 span 仍是人类口径的闭端
        series_sql = runner.calls[4]
        assert "'2023-11-01'" in series_sql and "'2024-02-01'" in series_sql
        assert "strftime" in series_sql
        # payload:series 节在;budget 未设置 → 不出现
        p = analysis_payload(out, question="q", chart=None,
                             baseline_label="", datasource="demo")
        assert p["series"]["labels"] == s["labels"]
        assert "budget" not in p["evidence"]

    async def test_confidence_interior_and_follows_k(self):
        """位置分数∈(0,1) 的中间档:k 由规格给,分数跟着同一公式走。"""
        runner = FakeRunner()
        out = await _engine(runner).run(
            _req(series=SeriesSpec(grain="month", lookback=3, k=6.0)))
        assert out is not None and out.series is not None
        z = 28.0 / (1.4826 * 2.0)
        assert out.series["k"] == 6.0
        assert out.series["confidence"] == pytest.approx((z - 6.0) / 6.0)
        assert 0.0 < out.series["confidence"] < 1.0

    async def test_limits_grain_default_also_enables(self):
        runner = FakeRunner()
        out = await _engine(runner, series_grain="month", block_lookback=3).run(_req())
        assert out is not None and out.series is not None
        assert out.series["lookback"] == 3

    async def test_unaligned_grain_degrades_and_delivers(self):
        runner = FakeRunner()
        # grain 留空 → 走 derive_grain(整季窗口无法对齐到月/周/日)
        out = await _engine(runner).run(
            _req(series=SeriesSpec(grain="", lookback=12),
                 time_context="2024-01-01 ~ 2024-03-31"))
        assert out is not None
        assert out.series is None
        assert {"stage": "series", "reason": "grain_unaligned"} in out.degraded
        # 主产物照常交付(序列是增强不是前提)
        assert out.table and out.partial is True

    async def test_empty_series_degrades(self):
        runner = FakeRunner(series_rows=[])
        out = await _engine(runner, series_grain="month").run(_req())
        assert out is not None and out.series is None
        assert {"stage": "series", "reason": "empty_series"} in out.degraded

    async def test_same_phase_filters_off_phase_buckets(self):
        runner = FakeRunner(series_rows=[
            ["2022-01", 10.0], ["2022-02", 11.0], ["2022-03", 12.0],
            ["2023-01", 13.0], ["2023-02", 14.0], ["2023-12", 15.0],
        ])
        out = await _engine(runner).run(
            _req(series=SeriesSpec(grain="month", lookback=2, mode="same_phase")))
        assert out is not None and out.series is not None
        # 只留 2 月相位(块 2022-02 / 2023-02);异相位桶剔除
        assert out.series["labels"] == ["2022-02", "2023-02"]
        assert out.series["values"] == [11.0, 14.0]
        assert out.series["mode"] == "same_phase"


class TestBudgetLedger:
    async def test_yield_accounted_not_silent(self):
        runner = FakeRunner()
        out = await _engine(runner, series_grain="month",
                            total_query_budget=4).run(_req())
        assert out is not None
        assert out.series is None
        assert {"stage": "series", "reason": "query_budget_exceeded"} in out.degraded
        assert out.budget == {
            "limit": 4, "used": 4, "by_stage": {"overall": 2, "probe": 2},
            "yielded": [{"stage": "series", "needed": 1,
                         "reason": "query_budget_exceeded", "remaining": 0}],
        }
        assert len(runner.calls) == 4          # 让路 = 真的没发查询
        p = analysis_payload(out, question="q", chart=None,
                             baseline_label="", datasource="demo")
        assert p["evidence"]["budget"]["used"] == 4

    async def test_snapshot_present_when_budget_set_and_used(self):
        runner = FakeRunner()
        out = await _engine(runner, series_grain="month",
                            total_query_budget=12).run(_req())
        assert out is not None and out.series is not None
        assert out.budget is not None
        assert out.budget["limit"] == 12 and out.budget["used"] == 5
        assert out.budget["by_stage"] == {"overall": 2, "probe": 2, "series": 1}
        assert out.budget["yielded"] == []


class TestBridgePath:
    async def test_run_components_runs_series_after_tree(self):
        runner = FakeRunner()
        engine = AnalysisEngine(_SL(), runner, AnalysisLimits(max_queries=20))
        out = await engine.run_components(
            _req(series=SeriesSpec(grain="month", lookback=3)))
        assert out is not None and out.series is not None
        purposes = [q["purpose"] for q in out.evidence_queries]
        assert purposes[-1] == "series"        # 树优先,序列垫底
        assert "driver_tree" in purposes
