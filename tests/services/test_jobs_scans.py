"""主动扫描任务 —— runner 的第三个分叉,与决策路径同一条纪律。

扫描任务与决策任务共享 job store、调度推进、冷却与投递,却共享不了别的:
它的正文由 ``ScanService`` 确定性产出(零 LLM)。三条性质在这里钉住:

1. **NL 管线绝不进入。** 扫描任务的 ``question`` 只是人类标签;它若漏进
   ``session_manager.ask``,运行的是一次 LLM 问答,而 run 行会记成一份
   看起来像扫描结论的答案。
2. **``advance`` 在失败路径上照跑。** 下一次运行时间不推进的任务会永远
   到期 —— 每个 tick 重跑一次、重发一次告警。
3. **失败响亮但不炸调度器。** 报告里的 ``error``(无 runner/无语义模型/
   坏窗口)折成 run 的 error 状态;LLM 自带的崩溃同理。

外加 store 侧:``scan_spec`` 是 jobs 表**最新的最后一列**(``_row_to_job``
按位置读),重开库必须各归各位 —— decision_rule / topic / scan_spec 三个
自由文本列错位不报错、只跑错东西。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from trove.services.jobs.runner import SchedulerRunner, _scan_verdict
from trove.services.jobs.service import JobsService
from trove.services.jobs.store import JobStore
from trove.services.scan.models import Finding
from trove.services.scan.service import ScanReport

NOW = datetime(2026, 9, 27, 10, 0, 0)

SPEC = {"metrics": ["loan_balance"], "dimensions": ["region"],
        "window": "本月", "lookback": 4}


class FakeSessionManager:
    """Records every ask — the assertion is that there are none."""

    def __init__(self):
        self.asked: list[str] = []

    async def start_session(self):
        return object()

    async def ask(self, session, question, workflow, datasource=None, topic=""):
        self.asked.append(question)
        raise AssertionError("a scan job must not ask the NL pipeline")

    async def resume(self, session, decision, workflow):
        raise AssertionError("a scan job must not resume the NL pipeline")


class FakeScans:
    """Duck-typed ScanService: scripted report (or explosion), records calls."""

    def __init__(self, report: ScanReport | None = None,
                 error: Exception | None = None):
        self.report = report if report is not None else _report()
        self.error = error
        self.calls: list[tuple] = []

    async def run(self, job, now=None):
        self.calls.append((job, now))
        if self.error is not None:
            raise self.error
        return self.report


class FakeSubs:
    """Records ``deliver_for_run`` — the scan report's outbound half."""

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    async def deliver_for_run(self, job, **kwargs):
        if self.fail:
            raise RuntimeError("delivery on fire")
        self.calls.append({"job": job, **kwargs})
        return []


def _finding(**over) -> Finding:
    base = {"metric": "loan_balance", "dimension": "region", "value": "华东",
            "window": "本月", "current": 500.0, "z": 26.98, "n": 4,
            "band": {"lo": 74.05, "hi": 125.95}}
    base.update(over)
    return Finding(**base)


def _report(**over) -> ScanReport:
    base = {
        "datasource": "demo", "spec": dict(SPEC), "window": "本月",
        "period": ["2026-09-01", "2026-09-30"], "units": 1,
        "findings": [_finding()],
        "drafts": [{"id": "scan-loan_balance-region-华东", "status": "created"}],
        "budget": {"used": 1, "total": 12},
    }
    base.update(over)
    return ScanReport(**base)


@pytest.fixture
async def svc(tmp_path):
    service = JobsService(JobStore(tmp_path))
    yield service
    await service.store.dispose()


async def _job(svc, **overrides):
    kwargs = {"question": "贷款余额扫描", "schedule": "5",
              "scan_spec": json.dumps(SPEC), "alert_channel": "console"}
    kwargs.update(overrides)
    return await svc.create_job(kwargs.pop("question"), kwargs.pop("schedule"),
                                "interval", **kwargs)


class TestScanVerdictLine:
    def test_counts_land_in_the_run_row(self):
        line = _scan_verdict(_report())
        assert line == "scan: 1 findings / 0 unverifiable / 1 drafts"

    def test_missing_attributes_do_not_crash_the_row(self):
        """:func:`_scan_verdict` 是 run 行的写法,不是判据 —— 形状怪也只截断。"""
        assert _scan_verdict(object()).startswith("scan: 0 findings")


class TestRunJobForks:
    async def test_scan_job_never_asks_the_nl_pipeline(self, svc):
        job = await _job(svc)
        manager = FakeSessionManager()          # raises if asked
        scans = FakeScans()
        runner = SchedulerRunner(manager, svc, scans=scans)

        summary = await runner.run_job(job, NOW)

        assert manager.asked == []
        assert scans.calls and scans.calls[0][0] is job
        assert summary["scan"] is True
        assert summary["status"] == "alert"     # 有发现 = 触发
        assert summary["row_count"] == 1

    async def test_decision_rule_wins_when_both_are_set(self, svc):
        """优先级是刻意的:decision_rule > scan_spec > NL。两栏都填时按
        决策跑 —— 规则引用是更明确的那一个声明。"""
        class _Decision:
            async def evaluate(self, rule, datasource, now=None, *, rule_digest=""):
                from trove.services.decision.service import DecisionOutcome

                return DecisionOutcome(triggered=False, message="",
                                       rule_id=rule.id)

        class _Kb:
            def load_decisions(self, datasource):
                from trove.services.decision.rules import DecisionDoc, DecisionRule

                return DecisionDoc(rules=[DecisionRule(
                    id="loan-drop", conditions=["delta_pct < -0.1"])])

        job = await _job(svc, decision_rule="loan-drop")
        scans = FakeScans()
        decision = _Decision()
        decision.kb = _Kb()
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 decision=decision, scans=scans)

        summary = await runner.run_job(job, NOW)

        assert scans.calls == []                # 扫描没跑
        assert summary.get("rule_id") == "loan-drop"

    async def test_missing_scan_service_is_an_error_not_a_fallback(self, svc):
        """没有扫描服务时落回 NL 管线,会用 LLM 去回答 ``job.question``
        那个标签 —— 产出一份看起来像扫描结论、其实什么都没扫的答案。"""
        job = await _job(svc)
        manager = FakeSessionManager()
        runner = SchedulerRunner(manager, svc, scans=None)
        summary = await runner.run_job(job, NOW)
        assert summary["status"] == "error"
        assert "scan service" in summary["error"]
        assert manager.asked == []

    async def test_parked_spec_falls_back_to_the_nl_path(self, svc):
        """空 scan_spec = 普通问答任务(老行为)。"""
        job = await svc.create_job("q", "5", "interval")

        class _Manager(FakeSessionManager):
            async def ask(self, session, question, workflow,
                          datasource=None, topic=""):
                from trove.workflow.state import WorkflowState

                self.asked.append(question)
                return WorkflowState(session_id="s1", question=question,
                                     columns=["a"], rows=[["1"]],
                                     row_count=1, verdict="OK")

        manager = _Manager()
        runner = SchedulerRunner(manager, svc, scans=FakeScans())
        summary = await runner.run_job(job, NOW)
        assert manager.asked == ["q"]
        assert "scan" not in summary


class TestScanRuns:
    async def test_no_findings_is_ok_not_an_error(self, svc):
        """"今天没有异常"是一个结果 —— 平局与"从没跑过"必须能分开。"""
        job = await _job(svc)
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 scans=FakeScans(_report(findings=[], drafts=[])))
        summary = await runner.run_job(job, NOW)

        assert summary["status"] == "ok"
        assert summary["row_count"] == 0
        assert summary["error"] == ""
        runs = await svc.store.list_runs(job.id)
        assert runs[0]["status"] == "ok"
        assert runs[0]["verdict"].startswith("scan: 0 findings")

    async def test_the_run_row_carries_the_report_evidence(self, svc):
        """扫描 run 没有 LangGraph 轨迹,run 行就是唯一能回答"扫了什么、
        发现什么、花了多少查询"的地方。"""
        job = await _job(svc)
        runner = SchedulerRunner(FakeSessionManager(), svc, scans=FakeScans())
        await runner.run_job(job, NOW)

        runs = await svc.store.list_runs(job.id)
        assert runs[0]["alert_triggered"] is True
        assert runs[0]["row_count"] == 1
        result = runs[0]["result"]
        assert result["findings"][0]["value"] == "华东"
        assert result["budget"]["used"] == 1
        assert result["period"] == ["2026-09-01", "2026-09-30"]

    async def test_report_error_lands_as_a_failed_run(self, svc):
        """设置类失败(无 runner/无语义模型/坏窗口)折在报告里 —— 折进
        run 的 error 状态,而不是抛给调度器。"""
        job = await _job(svc)
        report = _report(findings=[], drafts=[],
                         error="no semantic model for datasource 'demo'")
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 scans=FakeScans(report))
        before = job.next_run_at
        summary = await runner.run_job(job, NOW)

        assert summary["status"] == "error"
        assert "no semantic model" in summary["error"]
        assert summary["row_count"] == 0
        runs = await svc.store.list_runs(job.id)
        assert runs[0]["status"] == "error"
        # 失败路径上调度照推进(否则永远到期,每 tick 重发一次)
        reloaded = await svc.get_job(job.id)
        assert reloaded.next_run_at != before

    async def test_a_crashing_scan_still_advances_the_schedule(self, svc):
        job = await _job(svc)
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 scans=FakeScans(error=RuntimeError("boom")))
        before = job.next_run_at
        summary = await runner.run_job(job, NOW)

        assert summary["status"] == "error"
        assert "boom" in summary["error"]
        runs = await svc.store.list_runs(job.id)
        assert runs[0]["status"] == "error"
        assert (await svc.get_job(job.id)).next_run_at != before

    async def test_hypothesis_line_rides_in_the_report(self, svc):
        """假设轮的产出(含被拒记录)随报告进 run 行 —— 附录可回看。"""
        job = await _job(svc)
        report = _report(
            hypotheses=[{"hypothesis": {"claim": "c"}, "status": "supported"}],
            hypothesis_rejected=[{"claim": "x", "reason": "unknown_metric: nope"}],
        )
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 scans=FakeScans(report))
        await runner.run_job(job, NOW)

        result = (await svc.store.list_runs(job.id))[0]["result"]
        assert result["hypotheses"][0]["status"] == "supported"
        assert result["hypothesis_rejected"][0]["reason"].startswith("unknown_metric")


class TestScanDeliveryAndCooldown:
    async def test_the_scan_report_goes_to_subscribers(self, svc):
        job = await _job(svc)
        subs = FakeSubs()
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 scans=FakeScans(), subscriptions=subs)
        await runner.run_job(job, NOW)

        assert len(subs.calls) == 1
        call = subs.calls[0]
        assert call["status"] == "alert"
        assert call["alert_triggered"] is True
        assert call["report"]["scan"]["findings"][0]["value"] == "华东"
        assert call["report"]["question"] == "贷款余额扫描"

    async def test_delivery_failure_does_not_break_the_run(self, svc):
        """投递是 best-effort:一个收不到的订阅者绝不把跑好的扫描变成 error。"""
        job = await _job(svc)
        runner = SchedulerRunner(FakeSessionManager(), svc, scans=FakeScans(),
                                 subscriptions=FakeSubs(fail=True))
        summary = await runner.run_job(job, NOW)
        assert summary["status"] == "alert"
        assert (await svc.store.list_runs(job.id))[0]["status"] == "alert"

    async def test_cooldown_applies_to_scan_alerts(self, svc):
        """扫描与决策共用冷却:同一发现的第二次告警在窗口内只报告、不重发。"""
        job = await _job(svc, alert_cooldown_min=30)
        runner = SchedulerRunner(FakeSessionManager(), svc, scans=FakeScans())

        first = await runner.run_job(job, NOW)
        second = await runner.run_job(job, NOW)

        assert first["alert_sent"] is True
        assert second["status"] == "alert"
        assert second["alert_sent"] is False
        assert second["alert"]               # 仍然报告,只是不重发

    async def test_a_scan_error_does_not_enter_cooldown(self, svc):
        """error 不吃冷却 —— 否则一条坏配置会压掉紧随其后的第一条真告警。"""
        job = await _job(svc, alert_cooldown_min=30)
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 scans=FakeScans(_report(findings=[], drafts=[],
                                                         error="boom")))
        await runner.run_job(job, NOW)
        assert await svc._in_cooldown(job) is False


class TestStoreRoundTrip:
    async def test_all_three_text_columns_land_in_their_own_place(self, svc):
        """scan_spec 是表上最新的最后一列 —— 与 decision_rule/topic 一起
        重开库验证位置对齐(三个自由文本列错位不报错、只跑错东西)。"""
        job = await _job(svc, decision_rule="loan-drop", topic="loans")
        assert json.loads(job.scan_spec)["metrics"] == ["loan_balance"]

        reloaded = await svc.get_job(job.id)
        assert (reloaded.decision_rule, reloaded.topic) == ("loan-drop", "loans")
        assert json.loads(reloaded.scan_spec)["dimensions"] == ["region"]

    async def test_default_is_empty_so_old_jobs_stay_nl_jobs(self, svc):
        job = await svc.create_job("q", "5", "interval")
        assert (await svc.get_job(job.id)).scan_spec == ""

    async def test_update_and_clear(self, svc):
        job = await _job(svc)
        other = json.dumps({**SPEC, "top_k": 3})
        updated = await svc.update_job(job.id, scan_spec=other)
        assert json.loads(updated.scan_spec)["top_k"] == 3
        # None = 不动(与其余 update 字段同一语义)
        assert (await svc.update_job(job.id, name="n")).scan_spec == other
        assert (await svc.update_job(job.id, scan_spec="")).scan_spec == ""

    async def test_legacy_db_without_the_column_is_migrated(self, tmp_path):
        """上一版的 jobs 表(有 decision_rule/topic、没有 scan_spec)打开
        即补列;补不出列会让扫描任务静默退回 NL 问答。"""
        db = tmp_path / ".trove" / "jobs"
        db.mkdir(parents=True)
        import sqlite3

        conn = sqlite3.connect(db / "jobs.sqlite")
        conn.executescript("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, question TEXT NOT NULL,
                datasource TEXT DEFAULT 'demo', workflow TEXT DEFAULT 'reflection',
                schedule_type TEXT NOT NULL, schedule TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, alert_expr TEXT DEFAULT '',
                alert_channel TEXT DEFAULT '', alert_cooldown_min INTEGER NOT NULL DEFAULT 30,
                next_run_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                decision_rule TEXT DEFAULT '', topic TEXT DEFAULT '');
            INSERT INTO jobs VALUES ('legacy','n','old question','demo','reflection',
                'interval','5',1,'','',30,'','2026-01-01','2026-01-01','','');
        """)
        conn.commit()
        conn.close()

        svc = JobsService(JobStore(tmp_path))
        try:
            job = await svc.get_job("legacy")
            assert job is not None and job.scan_spec == ""
            await svc.update_job("legacy", scan_spec=json.dumps(SPEC))
            assert json.loads(
                (await svc.get_job("legacy")).scan_spec)["metrics"] == ["loan_balance"]
        finally:
            await svc.store.dispose()
