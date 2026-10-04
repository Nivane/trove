"""``scan.service`` —— 端到端(内存库 + 真实语义模型,零 LLM/网络)。

覆盖三段边界:

  1. ``scan()`` 纯确定性:窗口解析、逐单元、记账;
  2. ``scan_and_draft()`` 只落 **pending 草稿**(自动内容不绕过确认门),
     阈值取噪声带边而不是观测值;
  3. ``run(job)`` 的失败全部折进报告(响亮,不抛给调度器)。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from trove.services.decision.drafts import DecisionDraftStore
from trove.services.scan.models import ScanError, ScanSpec
from trove.services.scan.service import ScanReport, ScanService, _num_literal

NOW = datetime(2026, 9, 27, 10, 0, 0)


class _Job:
    def __init__(self, spec: str, datasource: str = "demo", jid: str = "j1"):
        self.id, self.datasource, self.scan_spec = jid, datasource, spec


@pytest.fixture
def svc(kb, runner, dialect_of):
    return ScanService(kb, runner=runner, dialect_of=dialect_of)


def _spec(**over) -> ScanSpec:
    base = {"metrics": ["loan_balance"], "dimensions": ["region"],
            "window": "本月", "lookback": 4}
    base.update(over)
    return ScanSpec.from_dict(base)


class TestNumLiteral:
    """条件语言的 NUMBER 是 ``\\d+(\\.\\d+)?`` —— 指数记法会解析失败。"""

    def test_no_scientific_notation(self):
        assert _num_literal(1e-05) == "0.00001"
        assert _num_literal(1.5e-07) == "0.00000015"
        assert "e" not in _num_literal(2.5e-06).lower()

    def test_trims_trailing_zeros(self):
        assert _num_literal(152.0) == "152"
        assert _num_literal(48.105) == "48.105"

    def test_non_finite_falls_back_to_zero(self):
        assert _num_literal(float("nan")) == "0"
        assert _num_literal("很多") == "0"


class TestResolveWindow:
    def test_chinese_window(self):
        assert ScanService.resolve_window("本月", NOW.date()) == (
            "2026-09-01", "2026-09-30")

    def test_english_window(self):
        assert ScanService.resolve_window("this month", NOW.date()) == (
            "2026-09-01", "2026-09-30")

    def test_unresolvable_is_none(self):
        assert ScanService.resolve_window("", NOW.date()) is None
        assert ScanService.resolve_window("很久以前", NOW.date()) is None


class TestScan:
    async def test_end_to_end_finds_the_spike(self, svc):
        report = await svc.scan(_spec(), "demo", NOW)
        assert report.error == ""
        assert report.period == ["2026-09-01", "2026-09-30"]
        assert report.grain == ""            # 由窗口推导,不在报告里回填猜测
        assert [f.value for f in report.findings] == ["华东"]
        assert report.unverifiable == []
        assert report.units == 1
        assert report.budget["used"] == 1    # 每单元恰好一条查询

    async def test_the_metric_only_view_scans_without_dimension(self, svc):
        report = await svc.scan(_spec(dimensions=[]), "demo", NOW)
        assert report.units == 1
        assert all(f.dimension == "" for f in report.findings)

    async def test_unresolvable_window_raises(self, svc):
        with pytest.raises(ScanError, match="not a resolvable time expression"):
            await svc.scan(_spec(window="很久以前"), "demo", NOW)

    async def test_missing_runner_is_loud(self, kb, dialect_of):
        svc = ScanService(kb, dialect_of=dialect_of)
        with pytest.raises(ScanError, match="no query runner"):
            await svc.scan(_spec(), "demo", NOW)

    async def test_missing_dialect_is_loud(self, kb, runner):
        svc = ScanService(kb, runner=runner)
        with pytest.raises(ScanError, match="no dialect resolver"):
            await svc.scan(_spec(), "demo", NOW)

    async def test_missing_semantic_model_is_loud(self, runner, dialect_of, tmp_path):
        from trove.services.kb.service import KbService

        empty = KbService(tmp_path / "empty")
        svc = ScanService(empty, runner=runner, dialect_of=dialect_of)
        with pytest.raises(ScanError, match="no semantic model"):
            await svc.scan(_spec(), "demo", NOW)

    async def test_budget_yields_are_recorded(self, kb, runner, dialect_of):
        from trove.core.config import ScanConfig

        svc = ScanService(kb, runner=runner, dialect_of=dialect_of,
                          config=type("C", (), {"scan": ScanConfig(max_queries=1)})())
        report = await svc.scan(
            _spec(metrics=["loan_balance", "loan_count"]), "demo", NOW)
        # 第一个单元花了唯一一次预算,第二个让路 —— 让路要记账,不静默
        assert report.units == 2
        assert report.budget["used"] == 1
        assert report.budget["yielded"][0]["stage"] == "scan"
        assert [f.reason for f in report.unverifiable] == ["query_budget_exceeded"]


class TestDrafts:
    async def test_finding_lands_as_a_pending_draft_with_the_band_edge(self, svc, kb):
        report = await svc.scan_and_draft(_spec(), "demo", NOW)
        assert [d["status"] for d in report.drafts] == ["created"]
        assert report.errors == []

        drafts = DecisionDraftStore(kb)
        pending = drafts.grouped("demo")["pending"]
        assert len(pending) == 1
        rule = pending[0]["rule"]
        assert rule["id"] == "scan-loan_balance-region-华东"
        assert rule["enabled"] is False          # 草稿默认停用(确认门)
        assert rule["subject"]["metrics"] == ["loan_balance"]
        assert rule["subject"]["filters"] == [
            {"field": "region", "op": "=", "value": "华东"}]
        # 阈值 = 噪声带**上边**(不是观测值 500):观测值阈值会自我实现。
        # 历史 4 块 100/110/90/100 → median 100、MAD_raw 5 → hi = 100 + 3.5·1.4826·5
        assert rule["conditions"] == ["current >= 125.9455"]
        assert "噪声带" in pending[0]["note"]

        # 执行面读不到:decisions.yml 不存在,规则不在执行面
        assert not kb.decisions_path("demo").exists()
        assert kb.load_decisions("demo").rules == []

    async def test_drafting_twice_is_idempotent(self, svc, kb):
        await svc.scan_and_draft(_spec(), "demo", NOW)
        again = await svc.scan_and_draft(_spec(), "demo", NOW)
        assert [d["status"] for d in again.drafts] == ["exists"]
        assert len(DecisionDraftStore(kb).grouped("demo")["pending"]) == 1

    async def test_no_findings_no_draft(self, svc, kb):
        report = await svc.scan_and_draft(_spec(window="上个月"), "demo", NOW)
        assert report.findings == []
        assert report.drafts == []
        assert not DecisionDraftStore(kb).path("demo").exists()


class TestRunJob:
    async def test_bad_spec_is_folded_into_the_report(self, svc):
        report = await svc.run(_Job("not json"), NOW)
        assert report.error.startswith("bad scan_spec:")
        assert report.findings == []

    async def test_structural_spec_error_is_folded(self, svc):
        report = await svc.run(_Job(json.dumps({"metrics": []})), NOW)
        assert "bad scan_spec:" in report.error

    async def test_scan_runs_and_drafts(self, svc, kb):
        report = await svc.run(
            _Job(json.dumps(_spec().to_dict())), NOW)
        assert report.error == ""
        assert [f.value for f in report.findings] == ["华东"]
        assert report.drafts[0]["status"] == "created"
        payload = report.to_dict()
        assert payload["findings"][0]["value"] == "华东"
        assert payload["budget"]["used"] == 1

    async def test_setup_failure_is_folded_not_raised(self, kb, dialect_of):
        """无 runner = 设置类失败:报告里一行 error,不炸调度器。"""
        svc = ScanService(kb, dialect_of=dialect_of)
        report = await svc.run(_Job(json.dumps(_spec().to_dict())), NOW)
        assert "no query runner" in report.error

    async def test_missing_semantic_model_is_one_error_line(
            self, runner, dialect_of, tmp_path):
        """没有 KB/语义模型 → 报告里一行设置类 error(响亮),不抛给调度器。"""
        svc = ScanService(None, tmp_path / "semantic", runner=runner,
                          dialect_of=dialect_of)
        report = await svc.run(_Job(json.dumps(_spec().to_dict())), NOW)
        assert isinstance(report, ScanReport)
        assert "no semantic model" in report.error


class TestHypothesisRound:
    async def test_off_by_default_for_scheduled_scans(self, svc):
        """``spec.hypotheses`` 默认 False → 定时扫描不花 LLM(成本面)。"""
        report = await svc.run(_Job(json.dumps(_spec().to_dict())), NOW)
        assert report.hypotheses == []
        assert report.hypothesis_rejected == []

    async def test_hypotheses_need_an_llm_gateway_and_that_is_recorded(self, svc):
        """开了假设开关但没接 LLM → 不静默:一行错误说明为什么没跑。"""
        spec = _spec(hypotheses=True).to_dict()
        report = await svc.run(_Job(json.dumps(spec)), NOW)
        assert report.hypotheses == []
        assert report.errors[0].startswith("hypotheses: no LLM gateway")

    async def test_round_runs_propose_then_verify(self, kb, runner, dialect_of):
        spec = _spec(hypotheses=True).to_dict()

        class _LLM:
            async def chat(self, model, messages, **kwargs):
                return json.dumps({"hypotheses": [{
                    "claim": "华东贷款余额上升",
                    "metric": "loan_balance", "dimension": "region",
                    "value": "华东", "direction": "up", "min_pct": 0.1,
                }]})

        svc = ScanService(kb, runner=runner, dialect_of=dialect_of, llm=_LLM())
        report = await svc.run(_Job(json.dumps(spec)), NOW)
        assert report.errors == []
        assert [h["status"] for h in report.hypotheses] == ["supported"]
        assert report.hypotheses[0]["observed"]["baseline"] >= 0
        assert report.to_dict()["hypotheses"][0]["status"] == "supported"

    async def test_hypothesis_failure_is_folded_not_raised(self, kb, runner, dialect_of):
        spec = _spec(hypotheses=True).to_dict()

        class _Boom:
            async def chat(self, model, messages, **kwargs):
                raise RuntimeError("llm down")

        svc = ScanService(kb, runner=runner, dialect_of=dialect_of, llm=_Boom())
        report = await svc.run(_Job(json.dumps(spec)), NOW)
        assert report.error == ""             # 扫描结论成立
        assert report.findings                 # 发现照出
        assert any(e.startswith("hypotheses:") for e in report.errors)
