"""JobsService + JobStore + alerting + runner tests (deterministic, tmp SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


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

    async def test_topic_and_rule_roundtrip_across_reopen(self, tmp_path):
        """topic 是 jobs 表当前的最后一列 —— 重开库必须各归各位。

        ``_row_to_job`` 按位置取值,列错位会把 decision_rule 读成 topic
        (或反过来),而两者都是自由文本,错位不报错、只答错范围。
        """
        svc = await self.make_service(tmp_path)
        job = await svc.create_job(
            "q", "5", "interval", decision_rule="loan-drop", topic="loans")
        assert (job.decision_rule, job.topic) == ("loan-drop", "loans")
        await svc.store.dispose()

        reopened = JobsService(JobStore(tmp_path))
        loaded = await reopened.get_job(job.id)
        assert (loaded.decision_rule, loaded.topic) == ("loan-drop", "loans")
        await reopened.store.dispose()

    async def test_topic_is_stripped_and_updatable(self, tmp_path):
        svc = await self.make_service(tmp_path)
        job = await svc.create_job("q", "5", "interval", topic="  loans ")
        assert job.topic == "loans"

        assert (await svc.update_job(job.id, topic="clients")).topic == "clients"
        # None = 不动(与其余 update 字段同一语义)
        assert (await svc.update_job(job.id, name="n")).topic == "clients"
        assert (await svc.update_job(job.id, topic="")).topic == ""


async def _run(job: Job):
    from trove.services.jobs.store import Run

    return Run(job_id=job.id)


class TestRunLifecycle:
    """The run row is *opened* running and *closed* terminal — no fake success.

    Before this state machine, ``start_run`` inserted the caller's status
    (``Run.status`` defaulted to ``"ok"``) before any work happened: a
    process killed mid-job left a run that every surface reported as a
    success.
    """

    async def test_start_run_writes_running_and_null_finished_at(self, tmp_path):
        from trove.services.jobs.store import Run

        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval")
        run_id = await svc.record_run(job, Run(job_id=job.id))
        recent = await svc.store.recent_run(job.id)
        assert recent["id"] == run_id
        assert recent["status"] == "running"
        assert recent["finished_at"] is None

    async def test_start_run_rejects_terminal_status(self, tmp_path):
        import pytest

        from trove.services.jobs.store import Run

        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval")
        with pytest.raises(ValueError):
            await svc.store.start_run(Run(job_id=job.id, status="ok"))
        with pytest.raises(ValueError):
            await svc.store.start_run(Run(job_id=job.id, finished_at="2026-10-27"))
        assert await svc.store.recent_run(job.id) is None

    async def test_finish_run_rejects_non_terminal_status(self, tmp_path):
        import pytest

        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval")
        run_id = await svc.record_run(job, await _run(job))
        with pytest.raises(ValueError):
            await svc.finish_run(run_id, "running", False, False, 0, "")
        # Still open: the rejected write changed nothing.
        assert (await svc.store.recent_run(job.id))["finished_at"] is None

    async def test_reconcile_closes_stranded_runs(self, tmp_path, caplog):
        """NULL (new) and '' (pre-upgrade) finished_at are both stranded;
        an existing verdict survives, an empty one becomes 'interrupted'."""
        from trove.services.jobs.store import Run

        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval")
        null_run = await svc.record_run(job, Run(job_id=job.id, verdict="OK"))
        # Pre-upgrade rows wrote '' before doing any work.
        empty_run = await svc.record_run(job, Run(job_id=job.id))
        await svc.store._backend.execute(
            "UPDATE runs SET finished_at='' WHERE id=?", (empty_run,),
        )
        await svc.store._backend.commit()

        with caplog.at_level("WARNING"):
            assert await svc.reconcile_interrupted_runs() == 2
        assert "reclaimed 2 interrupted run(s)" in caplog.text

        rows = {r["id"]: r for r in await svc.store.list_runs(job.id)}
        assert rows[null_run]["status"] == "error"
        assert rows[null_run]["finished_at"]
        assert rows[null_run]["verdict"] == "OK"       # existing verdict kept
        assert rows[empty_run]["status"] == "error"
        assert rows[empty_run]["verdict"] == "interrupted"

        # Idempotent: a second pass finds nothing stranded.
        assert await svc.reconcile_interrupted_runs() == 0

    async def test_reconcile_leaves_finished_runs_alone(self, tmp_path):
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval")
        run_id = await svc.record_run(job, await _run(job))
        await svc.finish_run(run_id, "alert", True, True, 7, "OK")
        assert await svc.reconcile_interrupted_runs() == 0
        recent = await svc.store.recent_run(job.id)
        assert (recent["status"], recent["verdict"], recent["row_count"]) == (
            "alert", "OK", 7,
        )


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

    async def test_cooldown_survives_the_runner_sequence(self, tmp_path):
        """The hand-built row above never catches this: in a real run the
        in-flight row is inserted *before* the verdict is known, so it is the
        newest row at check time. Asking `recent_run` for the cooldown anchor
        therefore always answered "the run you are inside" — which never
        alerts — and the dedup was silently a no-op."""
        from trove.services.jobs.runner import SchedulerRunner

        class _M:
            async def start_session(self, **kw):
                return object()

            async def ask(self, session, question, workflow, datasource=None,
                          topic=""):
                from trove.workflow.state import WorkflowState

                return WorkflowState(session_id="s", question=question,
                                     columns=["a"], rows=[["1"]], row_count=5,
                                     verdict="OK")

            async def resume(self, session, decision, workflow):
                raise AssertionError("not hitl")

        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job(
            "q", "5", "interval", alert_expr="row_count >= 3",
            alert_cooldown_min=30, alert_channel="console",
        )
        runner = SchedulerRunner(_M(), svc)
        first = await runner.run_job(job)
        second = await runner.run_job(job)
        assert first["alert_sent"] is True
        assert second["status"] == "alert"
        assert second["alert_sent"] is False

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
        self.asked_datasources = []
        self.asked_topics = []

    async def start_session(self, **kw):
        return object()

    async def ask(self, session, question, workflow, datasource=None, topic=""):
        self.asked.append(question)
        self.asked_datasources.append(datasource)
        self.asked_topics.append(topic)
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
        assert manager.asked_datasources == ["demo"]
        assert manager.asked_topics == [""]  # 不限定主题 → 空串,不是 None

    async def test_run_job_passes_job_datasource(self, tmp_path):
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval", datasource="financial")
        manager = FakeSessionManager([_final_state()])
        runner = SchedulerRunner(manager, svc)
        await runner.run_job(job)
        assert manager.asked_datasources == ["financial"]

    async def test_run_job_passes_job_topic_and_reports_it(self, tmp_path):
        """主题域随任务传进 NL 路径,并（有界地）落进运行报告。"""
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval", topic="loans")
        manager = FakeSessionManager([_final_state()])
        await SchedulerRunner(manager, svc).run_job(job)
        assert manager.asked_topics == ["loans"]
        report = (await svc.store.recent_run(job.id))["result"]
        assert report["topic"] == "loans"

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


# ── runner: 任务所有者身份(§2.4)────────────────────────


_USERS = {
    1: {"id": 1, "username": "admin", "role": "admin", "disabled": False},
    2: {"id": 2, "username": "bob", "role": "user", "disabled": False},
    3: {"id": 3, "username": "gone", "role": "user", "disabled": True},
}


class _AuthStore:
    def __init__(self, users):
        self._users = users

    async def get_user_by_id(self, user_id):
        return self._users.get(int(user_id))


class _Auth:
    """最小 AuthService 形状:runner 只用到 ``.store.get_user_by_id``。"""

    def __init__(self, users=_USERS):
        self.store = _AuthStore(users)


class _OwnerManager(FakeSessionManager):
    """记录每次运行以哪个 user_id 起会话;可选挂 auth 服务。"""

    def __init__(self, finals, auth=None):
        super().__init__(finals)
        if auth is not None:
            self._auth = auth
        self.user_ids = []

    async def start_session(self, user_id="local", **kw):
        self.user_ids.append(user_id)
        return object()


class TestRunnerOwner:
    async def test_owner_identity_reaches_the_session(self, tmp_path):
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval", owner_id="2")
        manager = _OwnerManager([_final_state()], auth=_Auth())
        summary = await SchedulerRunner(manager, svc).run_job(job)
        assert summary["status"] == "ok"
        # 非 admin owner 也是原样传入 —— 行/列 grant 由
        # SessionManager._principal_wire → Policy.principal_for 现读，
        # runner 不自己拼主体。
        assert manager.user_ids == ["2"]

    async def test_missing_owner_fails_loudly_with_auth(self, tmp_path):
        """启用多用户的部署里，存量空 owner 必须响亮失败 —— 静默按本地
        admin 运行正是本次要关的洞。"""
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval")
        manager = _OwnerManager([_final_state()], auth=_Auth())
        summary = await SchedulerRunner(manager, svc).run_job(job)
        assert summary["status"] == "error"
        assert "缺少所有者" in summary["error"]
        assert manager.asked == [] and manager.user_ids == []
        run = await svc.store.recent_run(job.id)
        assert run["status"] == "error"

    async def test_missing_owner_runs_locally_without_auth(self, tmp_path):
        """未启用 AuthService 的嵌入部署：本机可信身份，老路径不变。"""
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval")
        manager = _OwnerManager([_final_state()])
        summary = await SchedulerRunner(manager, svc).run_job(job)
        assert summary["status"] == "ok"
        assert manager.user_ids == ["local"]

    async def test_deleted_or_disabled_owner_fails(self, tmp_path):
        svc = JobsService(JobStore(tmp_path))
        for owner, needle in (("9", "不存在"), ("3", "已被禁用")):
            job = await svc.create_job("q", "5", "interval", owner_id=owner)
            manager = _OwnerManager([_final_state()], auth=_Auth())
            summary = await SchedulerRunner(manager, svc).run_job(job)
            assert summary["status"] == "error"
            assert needle in summary["error"]
            assert manager.asked == []

    async def test_non_numeric_owner_fails(self, tmp_path):
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job("q", "5", "interval", owner_id="local")
        manager = _OwnerManager([_final_state()], auth=_Auth())
        summary = await SchedulerRunner(manager, svc).run_job(job)
        assert summary["status"] == "error"
        assert "不是有效用户 id" in summary["error"]

    async def test_decision_job_requires_an_admin_owner(self, tmp_path):
        """系统任务不接个人 scope → 只接受 admin 所有者；非 admin 在碰
        decision 服务之前就被拦下。"""
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job(
            "label", "5", "interval", decision_rule="r1", owner_id="2")
        runner = SchedulerRunner(_OwnerManager([_final_state()], auth=_Auth()), svc)
        summary = await runner.run_job(job)
        assert summary["status"] == "error"
        assert "不是管理员" in summary["error"]

    async def test_decision_job_admin_owner_passes_the_owner_gate(self, tmp_path):
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job(
            "label", "5", "interval", decision_rule="r1", owner_id="1")
        runner = SchedulerRunner(_OwnerManager([_final_state()], auth=_Auth()), svc)
        summary = await runner.run_job(job)
        # 越过 owner 门后撞上的下一道:没有注入 decision 服务。断言它
        # **不是** owner 错误 —— 证明 admin owner 被放行。
        assert summary["status"] == "error"
        assert "所有者" not in summary["error"]

    async def test_scan_job_requires_an_admin_owner(self, tmp_path):
        svc = JobsService(JobStore(tmp_path))
        job = await svc.create_job(
            "label", "5", "interval",
            scan_spec='{"metrics": ["revenue"]}', owner_id="2")
        runner = SchedulerRunner(_OwnerManager([_final_state()], auth=_Auth()), svc)
        summary = await runner.run_job(job)
        assert summary["status"] == "error"
        assert "不是管理员" in summary["error"]


class TestStoreE2E:
    async def test_no_alert_no_channel_defaults_console(self, tmp_path):
        svc = JobsService(JobStore(tmp_path))
        job = Job(name="n", question="q", schedule="5", schedule_type="interval")
        await svc.store.save_job(job)
        loaded = await svc.list_jobs()
        assert loaded[0].question == "q"
        assert loaded[0].enabled is True