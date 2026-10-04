"""``scan.scanner`` —— 确定性扫描的单元面(假语义层 + 假 runner)。

三条纪律各有对应用例:

  1. **当期块不进噪声带** —— 断言带只由历史块构成(把被测点混进分布
     会自我稀释);
  2. **算不出 ≠ 没问题** —— 每种算不出恰好一行 ``unverifiable``,
     reason 可读。静默少一行,与「今天一切正常」从产物上看没有区别;
  3. **确定性排序/截断** —— ``rank_findings`` 与 ``cap_findings``
     只截 anomaly,判不了的一行不丢。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from trove.services.analysis.budget import QueryLedger
from trove.services.scan import scanner
from trove.services.scan.models import ScanSpec
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
)

NOW = datetime(2026, 9, 27, 10, 0, 0)
CUR = ("2026-09-01", "2026-09-30")


def _model() -> SemanticModel:
    return SemanticModel(
        name="m",
        datasets=[SemanticDataset(
            name="loan", source="loan",
            fields=[
                SemanticField(name="amount", expression="loan.amount",
                              semantic_role="measure"),
                SemanticField(name="region", expression="loan.region",
                              semantic_role="dimension"),
                SemanticField(name="date", expression="loan.date",
                              datatype="DATE", is_time=True),
            ],
        )],
        metrics=[SemanticMetric(
            name="loan_balance", expression="SUM(loan.amount)", datasets=["loan"],
            agg_time_dimension="loan.date")],
    )


class _SL:
    def __init__(self, model: SemanticModel | None = None) -> None:
        self._model = model if model is not None else _model()

    def model(self) -> SemanticModel:
        return self._model


def _no_time_model() -> SemanticModel:
    """把时间维度从每一层声明里摘干净(flag / role / datatype 三处)。"""
    model = _model()
    for f in model.datasets[0].fields:
        f.is_time = False
        if str(f.semantic_role or "").lower() == "time":
            f.semantic_role = ""
        if str(f.datatype or "").upper() in ("DATE", "DATETIME", "TIMESTAMP"):
            f.datatype = ""
    model.metrics[0].agg_time_dimension = ""
    return model


def _spec(**over) -> ScanSpec:
    base = {"metrics": ["loan_balance"], "dimensions": ["region"],
            "window": "本月", "lookback": 4}
    base.update(over)
    return ScanSpec.from_dict(base)


class _Runner:
    """脚本化 runner:记录 SQL,按脚本出 (列, 行) 或抛错。"""

    def __init__(self, columns, rows, error: Exception | None = None):
        self.columns, self.rows, self.error = columns, rows, error
        self.sqls: list[str] = []

    async def __call__(self, sql: str, datasource: str):
        self.sqls.append(sql)
        if self.error is not None:
            raise self.error
        return list(self.columns), list(self.rows)


async def _scan(sl, runner, spec, *, ledger=None, evidence=None):
    metric_name, dim_name = spec.units[0]      # 单元 = 规格声明的第一个
    return await scanner.scan_unit(
        semantic_layer=sl, runner=runner, datasource="demo", dialect="sqlite",
        spec=spec, metric_name=metric_name, dim_name=dim_name,
        cur_window=CUR, ledger=ledger or QueryLedger(),
        evidence=evidence if evidence is not None else [],
    )


class TestAnomaly:
    async def test_group_outside_the_band_becomes_an_anomaly(self):
        runner = _Runner(["region", "bucket", "loan_balance"], [
            ["华东", "2026-05", 100.0], ["华东", "2026-06", 110.0],
            ["华东", "2026-07", 90.0], ["华东", "2026-08", 100.0],
            ["华东", "2026-09", 500.0],
            ["华北", "2026-05", 50.0], ["华北", "2026-06", 52.0],
            ["华北", "2026-07", 48.0], ["华北", "2026-08", 50.0],
            ["华北", "2026-09", 52.0],
        ])
        found = await _scan(_SL(), runner, _spec())
        assert [f.value for f in found] == ["华东"]
        f = found[0]
        assert f.kind == "anomaly" and f.outside is True
        assert f.current == 500.0
        assert f.period == ["2026-09", "2026-09"]
        assert f.n == 4                       # 带只由 4 个历史块构成
        assert f.grain == "month" and f.mode == "trailing"
        # 带心 = 中位数 100,当期 500 绝不参与;scale = MAD(5) × 1.4826
        assert f.band["center"] == pytest.approx(100.0)
        assert f.z == pytest.approx((500 - 100) / (5 * 1.4826))
        assert f.low_n is True                # n=4 < LOW_N=12,标必须打上

    async def test_in_band_group_produces_nothing(self):
        runner = _Runner(["region", "bucket", "loan_balance"], [
            ["华北", "2026-05", 50.0], ["华北", "2026-06", 52.0],
            ["华北", "2026-07", 48.0], ["华北", "2026-08", 50.0],
            ["华北", "2026-09", 52.0],
        ])
        assert await _scan(_SL(), runner, _spec()) == []

    async def test_the_series_query_covers_history_and_current_window(self):
        runner = _Runner(["region", "bucket", "loan_balance"], [
            ["华北", "2026-05", 50.0],
        ])
        await _scan(_SL(), runner, _spec())
        sql = runner.sqls[0]
        # 半开窗口:起于首个历史块,终于当期窗口末 + 1 天(不含)
        assert "2026-05-01" in sql and "2026-10-01" in sql

    async def test_missing_current_block_is_not_a_zero(self):
        """当期无行 = 数据可能没到,不是"掉到零"的证据 → 判不了,不判超带。"""
        runner = _Runner(["region", "bucket", "loan_balance"], [
            ["华东", "2026-05", 100.0], ["华东", "2026-06", 110.0],
            ["华东", "2026-07", 90.0], ["华东", "2026-08", 100.0],
        ])
        found = await _scan(_SL(), runner, _spec())
        assert len(found) == 1
        assert found[0].kind == "unverifiable"
        assert found[0].reason == "no_current_block"
        assert found[0].current is None

    async def test_zero_spread_band_is_unverifiable_not_silent(self):
        """常数历史 → MAD=0 → 带不可用(判不了),绝不当作"没超带"。"""
        runner = _Runner(["region", "bucket", "loan_balance"], [
            ["华东", "2026-05", 100.0], ["华东", "2026-06", 100.0],
            ["华东", "2026-07", 100.0], ["华东", "2026-08", 100.0],
            ["华东", "2026-09", 500.0],
        ])
        found = await _scan(_SL(), runner, _spec())
        assert [f.reason for f in found] == ["band_unavailable"]
        assert found[0].kind == "unverifiable"


class TestUnverifiableIsAlwaysOneRow:
    """每一种算不出都恰好一行,带可读 reason —— 一行都不能静默消失。"""

    async def test_unknown_metric(self):
        found = await _scan(_SL(), _Runner([], []), _spec(metrics=["nope"]))
        assert [f.reason for f in found] == ["unknown_metric"]

    async def test_unknown_dimension(self):
        found = await _scan(_SL(), _Runner([], []), _spec(dimensions=["nope"]))
        assert [f.reason for f in found] == ["unknown_dimension"]

    async def test_no_anchor_dataset(self):
        model = _model()
        model.metrics[0].datasets = []
        found = await _scan(_SL(model), _Runner([], []), _spec())
        assert [f.reason for f in found] == ["no_anchor_dataset"]

    async def test_no_time_field(self):
        found = await _scan(_SL(_no_time_model()), _Runner([], []), _spec())
        assert [f.reason for f in found] == ["no_time_field"]

    async def test_grain_unaligned(self):
        """窗口不是整块(半月)→ 不猜粒度,判不了。"""
        found = await scanner.scan_unit(
            semantic_layer=_SL(), runner=_Runner([], []), datasource="demo",
            dialect="sqlite", spec=_spec(), metric_name="loan_balance",
            dim_name="region", cur_window=("2026-09-01", "2026-09-15"),
            ledger=QueryLedger(), evidence=[])
        assert [f.reason for f in found] == ["grain_unaligned"]

    async def test_no_window(self):
        found = await scanner.scan_unit(
            semantic_layer=_SL(), runner=_Runner([], []), datasource="demo",
            dialect="sqlite", spec=_spec(), metric_name="loan_balance",
            dim_name="region", cur_window=None,
            ledger=QueryLedger(), evidence=[])
        assert [f.reason for f in found] == ["no_window"]

    async def test_query_failure_is_a_row_not_a_crash_and_is_not_recorded(self):
        ledger = QueryLedger(total=5)
        found = await _scan(
            _SL(), _Runner([], [], error=RuntimeError("connection reset")),
            _spec(), ledger=ledger)
        assert len(found) == 1 and found[0].kind == "unverifiable"
        assert found[0].reason.startswith("query_failed: connection reset")
        assert found[0].sql  # 失败的 SQL 也留证据(不然没法复现)
        assert ledger.used == 0             # 没执行成功就不记账

    async def test_budget_exhaustion_yields_with_a_reason(self):
        ledger = QueryLedger(total=0)
        found = await _scan(_SL(), _Runner([], []), _spec(), ledger=ledger)
        assert [f.reason for f in found] == ["query_budget_exceeded"]
        assert ledger.yielded[0]["stage"] == "scan"
        assert ledger.yielded[0]["reason"] == "query_budget_exceeded"

    async def test_empty_series(self):
        found = await _scan(_SL(), _Runner(["region", "bucket", "loan_balance"], []),
                            _spec())
        assert [f.reason for f in found] == ["empty_series"]

    async def test_multi_group_issues_are_counted_not_dropped(self):
        """一组超带 + 另一组判不了 → 发现照出,判不了记进证据 notes。"""
        runner = _Runner(["region", "bucket", "loan_balance"], [
            ["华东", "2026-05", 100.0], ["华东", "2026-06", 110.0],
            ["华东", "2026-07", 90.0], ["华东", "2026-08", 100.0],
            ["华东", "2026-09", 500.0],
            ["华北", "2026-05", 50.0], ["华北", "2026-06", 52.0],
            ["华北", "2026-07", 48.0], ["华北", "2026-08", 50.0],
        ])
        evidence: list[dict] = []
        found = await _scan(_SL(), runner, _spec(), evidence=evidence)
        assert [f.value for f in found] == ["华东"]
        assert evidence[-1]["notes"] == [{"reason": "no_current_block", "groups": 1}]


class TestSpecIssues:
    """写时校验(API/CLI 用):引用解析不过 = 干脆别写进任务。"""

    def test_clean_spec_has_no_issues(self):
        assert scanner.spec_issues(_SL(), _spec()) == []

    def test_unknown_metric(self):
        issues = scanner.spec_issues(_SL(), _spec(metrics=["nope"]))
        assert len(issues) == 1 and "nope" in issues[0]

    def test_unknown_dimension(self):
        issues = scanner.spec_issues(_SL(), _spec(dimensions=["nope"]))
        assert len(issues) == 1 and "nope" in issues[0]

    def test_no_time_field_is_caught_at_write_time(self):
        issues = scanner.spec_issues(_SL(_no_time_model()), _spec())
        assert len(issues) == 1 and "time field" in issues[0]

    def test_every_broken_unit_is_listed(self):
        issues = scanner.spec_issues(
            _SL(), _spec(metrics=["nope"], dimensions=["nope"]))
        assert len(issues) == 1                    # 坏的度量直接定案,不继续猜维度


class TestRankingAndCap:
    def _f(self, value, z, kind="anomaly"):
        from trove.services.scan.models import Finding
        return Finding(metric="m", dimension="d", value=value, kind=kind, z=z)

    def test_rank_is_z_desc_then_name(self):
        fs = [self._f("a", 2.0), self._f("b", 9.0), self._f("c", None)]
        ranked = scanner.rank_findings(fs)
        assert [f.value for f in ranked] == ["b", "a", "c"]

    def test_unverifiable_always_after_anomalies(self):
        fs = [self._f("u", 99.0, kind="unverifiable"), self._f("a", 1.0)]
        assert [f.value for f in scanner.rank_findings(fs)] == ["a", "u"]

    def test_rank_is_input_order_independent(self):
        fs = [self._f("b", 9.0), self._f("a", 1.0)]
        assert scanner.rank_findings(fs) == scanner.rank_findings(list(reversed(fs)))

    def test_cap_only_truncates_anomalies(self):
        fs = [self._f("a", 9.0), self._f("b", 8.0), self._f("c", 7.0),
              self._f("u1", None, kind="unverifiable"),
              self._f("u2", None, kind="unverifiable")]
        kept, dropped = scanner.cap_findings(fs, 1)
        assert [f.value for f in kept] == ["a", "u1", "u2"]
        assert dropped == 2
