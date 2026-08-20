"""JobsService + JobStore + alerting + runner tests (deterministic, tmp SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trove.services.jobs.alerts import evaluate_alert
from trove.services.jobs.notify import build_notifier, ConsoleNotifier, WebhookNotifier
from trove.services.jobs.runner import SchedulerRunner
from trove.services.jobs.service import JobsService
from trove.services.jobs.store import Job, JobStore


# ── alert expressions ───────────────────────────────────


class TestAlertEval:
    def test_row_count_comparisons(self):
        assert evaluate_alert("row_count >= 3", row_count=3).triggered
        assert evaluate_alert("row_count > 3", row_count=3).triggered is False
        assert evaluate_alert("row_count == 0", row_count=0).triggered

    def test_no_rows(self):
        assert evaluate_alert("no_rows", row_count=0).triggered
        assert evaluate_alert("empty", row_count=1).triggered is False

    def test_value_and_column(self):
        rows = [["east", "1200"], ["west", "800"]]
        assert evaluate_alert("value > 1000", rows=rows).triggered
        assert evaluate_alert("col:amount >= 1000", columns=["region", "amount"], rows=rows).triggered
        assert evaluate_alert("col:amount < 1000", columns=["region", "amount"], rows=rows).triggered is False

    def test_verdict_string(self):
        assert evaluate_alert("verdict == RETRY", verdict="RETRY").triggered
        assert evaluate_alert('verdict == "OK"', verdict="RETRY").triggered is False

    def test_unknown_column_safe_false(self):
        assert evaluate_alert("col:missing > 5", columns=["a"], rows=[["1"]]).triggered is False

    def test_garbage_safe_false(self):
        assert evaluate_alert("not an expression at all", row_count=5).triggered is False
        assert evaluate_alert("", row_count=5).triggered is False


class TestNotifiers:
    def test_build_notifier(self):
        assert isinstance(build_notifier("console"), ConsoleNotifier)
        assert isinstance(build_notifier("webhook:https://x.test/hook"), WebhookNotifier)
        assert build_notifier("unknown") is None
        assert build_notifier("webhook:not-a-url") is None
        assert build_notifier("") is None


# ── job lifecycle ───────────────────────────────────────


class TestJobsService:
    async def make_service(self, tmp_path) -> JobsService:
        return JobsService(JobStore(tmp_path))

    async def test_create_interval_job(self, tmp_path):
        svc = await self.make_service(tmp_path)
        job = await svc.create_job("月贷款总量是多少", "30", "interval", datasource="demo")
        assert job is not None
        assert job.schedule_type == "interval"
        assert job.next_run_at

    async def test_create_cron_job(self, tmp_path):
        svc = await self.make_service(tmp_path)
        job = await svc.create_job(
            "每日贷款总量", "0 9 * * *", "cron",
            alert_expr="row_count >= 5", alert_channel="console",
        )
        assert job is not None
        crons = await svc.list_jobs()
        assert crons[0].alert_expr == "row_count >= 5"

    async def test_create_rejects_invalid(self, tmp_path):
        svc = await self.make_service(tmp_path)
        assert await svc.create_job("", "30", "interval") is None
        assert await svc.create_job("q", "99 99 99 99 99", "cron") is None
        assert await svc.create_job("q", "30", "bogus") is None

    async def test_due_jobs_and_advance(self, tmp_path):
        from dataclasses import replace

        svc = await self.make_service(tmp_path)
        job = await svc.create_job("早上汇总", "1", "interval")
        due = await svc.due_jobs()
        # next_run in ~1min; force due by backdating
        stale = replace(job, enabled=True, next_run_at=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat())
        await svc.store.save_job(stale)
        due = await svc.due_jobs()
        assert [j.id for j in due] == [stale.id]
        nxt = await svc.advance(due[0])
        assert nxt  # advanced to next minute

    async def test_toggle_disable(self, tmp_path):
        svc = await self.make_service(tmp_path)
        job = await svc.create_job("q", "5", "interval")
        off = await svc.toggle(job.id, False)
        assert off.enabled is False
        assert off.next_run_at == ""
        assert await svc.due_jobs() == []

    async def test_cancel(self, tmp_path):
        svc = await self.make_service(tmp_path)
        job = await svc.create_job("q", "5", "interval")
        assert await svc.cancel(job.id) is True
        assert await svc.get_job(job.id) is None

    async def test_store_run_persistence(self, tmp_path):
        svc = await self.make_service(tmp_path)
        job = await svc.create_job("q", "5", "interval")
        run_id = await svc.record_run(job, await _run(job))
        await svc.finish_run(run_id, "ok", False, False, 3, "OK")
        recent = await svc.store.recent_run(job.id)
        assert recent["status"] == "ok"
        assert recent["row_count"] == 3


async def _run(job: Job):
    from trove.services.jobs.store import Run

    return Run(job_id=job.id)


class TestCooldown:
    async def test_alert_dedup_within_cooldown(self, tmp_path):
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job(
            "q", "5", "interval", alert_expr="row_count >= 3", alert_cooldown_min=30,
        )
        # recent alert just fired
        from trove.services.jobs.store import Run

        run_id = await svc.record_run(job, Run(job_id=job.id))
        await svc.finish_run(run_id, "alert", True, True, 5, "OK")
        ev = await svc.evaluate(job, {"columns": [], "rows": [], "row_count": 5})
        assert ev["triggered"] is True
        assert ev["notify"] is False  # cooldown suppresses notify

    async def test_alert_notify_outside_cooldown(self, tmp_path):
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval", alert_expr="row_count >= 3")
        # no prior run → notify allowed
        ev = await svc.evaluate(job, {"columns": [], "rows": [], "row_count": 3})
        assert ev["triggered"] is True
        assert ev["notify"] is True
        # dispatch should work for console channel
        ok = await svc.dispatch(job, ev["message"], {})
        assert ok is True


# ── runner (fake session manager) ───────────────────────


class FakeSessionManager:
    """Duck-typed SessionManager: records asks, returns preset finals."""

    def __init__(self, finals):
        self._finals = list(finals)
        self.asked = []

    async def start_session(self):
        return object()

    async def ask(self, session, question, workflow):
        self.asked.append(question)
        return self._finals.pop(0) if self._finals else self._finals[-1]

    async def resume(self, session, decision, workflow):
        return self._finals.pop(0) if self._finals else self._finals[-1]


def _final_state(**overrides):
    from trove.workflow.state import WorkflowState

    defaults = {
        "session_id": "s1", "question": "q",
        "columns": ["region", "amount"], "rows": [["east", 1200]],
        "row_count": 1, "verdict": "OK",
    }
    defaults.update(overrides)
    return WorkflowState(**defaults)


class TestRunner:
    async def test_run_job_no_alert(self, tmp_path):
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval")
        manager = FakeSessionManager([_final_state()])
        runner = SchedulerRunner(manager, svc)
        summary = await runner.run_job(job)
        assert summary["status"] == "ok"
        assert summary["row_count"] == 1
        assert summary["alert"] == ""
        assert manager.asked == ["q"]

    async def test_run_job_triggers_alert(self, tmp_path):
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job(
            "q", "5", "interval", alert_expr="value > 1000", alert_channel="console",
        )
        runner = SchedulerRunner(FakeSessionManager([_final_state()]), svc)
        summary = await runner.run_job(job)
        assert summary["status"] == "alert"
        assert summary["alert"][:5] == "value"
        assert summary["alert_sent"] is True

    async def test_run_job_error_finishes_as_error(self, tmp_path):
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval")
        runner = SchedulerRunner(FakeSessionManager([_final_state(error="boom")]), svc)
        summary = await runner.run_job(job)
        assert summary["status"] == "error"
        assert summary["error"] == "boom"

    async def test_tick_runs_all_due(self, tmp_path, monkeypatch):
        from dataclasses import replace

        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval")
        stale = replace(job, next_run_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
        await svc.store.save_job(stale)
        runner = SchedulerRunner(FakeSessionManager([_final_state()]), svc)
        results = await runner.tick()
        assert len(results) == 1
        assert results[0]["status"] == "ok"


class TestStoreE2E:
    async def test_no_alert_no_channel_defaults_console(self, tmp_path):
        svc = JobsService(JobStore(tmp_path))
        job = Job(name="n", question="q", schedule="5", schedule_type="interval")
        await svc.store.save_job(job)
        loaded = await svc.list_jobs()
        assert loaded[0].question == "q"
        assert loaded[0].enabled is True