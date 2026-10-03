"""定时分析 + 订阅：store / SubscriptionService / runner 投递测试。

零 LLM、零网络、tmp SQLite。通道一律换成记录型 fake（真 ConsoleNotifier
会 print，真 WebhookNotifier 要联网），断言落在「选了哪条通道、投了什么、
落了什么投递记录」这些确定性事实上。
"""

from __future__ import annotations

import pytest

from trove.services.jobs import subscribe as subscribe_mod
from trove.services.jobs.runner import SchedulerRunner
from trove.services.jobs.service import JobsService
from trove.services.jobs.store import Delivery, Job, JobStore
from trove.services.jobs.subscribe import SubscriptionService


# ── fixtures ────────────────────────────────────────────


@pytest.fixture
async def store(tmp_path):
    s = JobStore(tmp_path)
    yield s
    await s.dispose()


@pytest.fixture
async def jobs(store):
    return JobsService(store)


@pytest.fixture
async def subs(store):
    return SubscriptionService(store)


class RecordingNotifier:
    def __init__(self, ok: bool = True, error: Exception | None = None):
        self.ok = ok
        self.error = error
        self.sent: list[dict] = []

    async def send(self, payload):
        if self.error is not None:
            raise self.error
        self.sent.append(payload)
        return self.ok


@pytest.fixture
def notifier(monkeypatch):
    """Replace build_notifier: known channels → one recording notifier,
    unknown → None (mirrors the real factory's accept/reject rule)."""
    rec = RecordingNotifier()

    def factory(channel):
        ch = (channel or "").strip()
        if ch == "console":
            return rec
        if ch.lower().startswith("webhook:"):
            url = ch.split(":", 1)[1].strip()
            if url.startswith(("http://", "https://")):
                return rec
        return None

    monkeypatch.setattr(subscribe_mod, "build_notifier", factory)
    return rec


async def _job(jobs: JobsService, **kw) -> Job:
    defaults = dict(datasource="demo")
    defaults.update(kw)
    job = await jobs.create_job("月贷款总量是多少", "30", "interval", **defaults)
    assert job is not None
    return job


# ── store ───────────────────────────────────────────────


class TestSubscriptionStore:
    async def test_roundtrip_and_update(self, store):
        from trove.services.jobs.store import Subscription

        sub = Subscription(job_id="j1", subscriber="bob", created_by="admin")
        await store.save_subscription(sub)
        loaded = await store.get_subscription(sub.id)
        assert loaded is not None
        assert (loaded.subscriber, loaded.mode, loaded.enabled) == ("bob", "always", True)

        loaded.mode = "alert_only"
        loaded.enabled = False
        await store.save_subscription(loaded)
        again = await store.get_subscription(sub.id)
        assert again.mode == "alert_only"
        assert again.enabled is False
        # ON CONFLICT(id) — 更新不产生第二行
        assert len(await store.load_subscriptions()) == 1

    async def test_delete(self, store):
        from trove.services.jobs.store import Subscription

        sub = Subscription(job_id="j1", subscriber="bob")
        await store.save_subscription(sub)
        assert await store.delete_subscription(sub.id) is True
        assert await store.delete_subscription(sub.id) is False
        assert await store.get_subscription(sub.id) is None

    async def test_delivery_idempotent_per_run(self, store):
        d = Delivery(subscription_id="s1", job_id="j1", run_id=7,
                     subscriber="bob", channel="console", excerpt="x")
        await store.add_delivery(d)
        await store.add_delivery(d)  # 同 (sub, run) → DO NOTHING
        rows = await store.list_deliveries()
        assert len(rows) == 1
        assert rows[0]["status"] == "sent"

        await store.add_delivery(Delivery(
            subscription_id="s1", job_id="j1", run_id=8, subscriber="bob"))
        assert len(await store.list_deliveries(subscription_id="s1")) == 2

    async def test_delivery_filters(self, store):
        for sub_id, who in (("s1", "bob"), ("s2", "carol")):
            await store.add_delivery(Delivery(
                subscription_id=sub_id, job_id="j1", run_id=1, subscriber=who))
        assert len(await store.list_deliveries(subscriber="bob")) == 1
        assert len(await store.list_deliveries(job_id="j1")) == 2
        assert len(await store.list_deliveries(job_id="j2")) == 0


# ── service ─────────────────────────────────────────────


class TestSubscriptionService:
    async def test_create_validates(self, subs):
        assert await subs.create("j1", "") is None
        assert await subs.create("j1", "bob", mode="bogus") is None
        sub = await subs.create("j1", " bob ", channel="", created_by="admin")
        assert sub is not None and sub.subscriber == "bob"

    async def test_find_and_list(self, subs):
        a = await subs.create("j1", "bob")
        b = await subs.create("j2", "bob")
        assert (await subs.find("j1", "bob")).id == a.id
        assert await subs.find("j1", "carol") is None
        assert len(await subs.list_subs(subscriber="bob")) == 2
        assert [s.id for s in await subs.list_subs(job_id="j2")] == [b.id]

    async def test_update(self, subs):
        sub = await subs.create("j1", "bob")
        assert await subs.update("nope", enabled=False) is None
        assert await subs.update(sub.id, mode="bogus") is None
        updated = await subs.update(sub.id, channel="console", mode="alert_only",
                                   enabled=False)
        assert (updated.channel, updated.mode, updated.enabled) == (
            "console", "alert_only", False)

    async def test_delete(self, subs):
        sub = await subs.create("j1", "bob")
        assert await subs.delete(sub.id) is True
        assert await subs.delete(sub.id) is False


class TestDelivery:
    async def test_always_mode_delivers_and_records(self, jobs, subs, notifier):
        job = await _job(jobs)
        await subs.create(job.id, "bob", created_by="admin")
        results = await subs.deliver_for_run(
            job, run_id=1, status="ok", verdict="OK", alert_triggered=False,
            alert_message="", report={"answer": "本月贷款总量 120 万元",
                                      "question": job.question},
        )
        assert [r["status"] for r in results] == ["sent"]
        assert results[0]["channel"] == "console"  # 无通道 → 默认 console
        payload = notifier.sent[0]
        assert payload["kind"] == "REPORT"
        assert payload["type"] == "trove.report"
        assert payload["subscriber"] == "bob"
        assert payload["message"] == "本月贷款总量 120 万元"
        assert payload["report"]["answer"] == "本月贷款总量 120 万元"
        rows = await subs.list_deliveries(subscription_id=results[0]["subscription_id"])
        assert len(rows) == 1
        assert rows[0]["excerpt"] == "本月贷款总量 120 万元"
        assert rows[0]["run_id"] == 1

    async def test_alert_only_skipped_unless_triggered(self, jobs, subs, notifier):
        job = await _job(jobs)
        await subs.create(job.id, "bob", mode="alert_only")
        results = await subs.deliver_for_run(
            job, run_id=1, status="ok", verdict="", alert_triggered=False,
            alert_message="", report={"answer": "一切正常"},
        )
        assert results == []
        assert notifier.sent == []
        assert await subs.list_deliveries() == []

        results = await subs.deliver_for_run(
            job, run_id=2, status="alert", verdict="", alert_triggered=True,
            alert_message="row_count >= 3 触发", report={"answer": "3 行"},
        )
        assert [r["status"] for r in results] == ["sent"]
        assert notifier.sent[0]["message"] == "row_count >= 3 触发"

    async def test_disabled_skipped(self, jobs, subs, notifier):
        job = await _job(jobs)
        sub = await subs.create(job.id, "bob")
        await subs.update(sub.id, enabled=False)
        results = await subs.deliver_for_run(
            job, run_id=1, status="ok", verdict="", alert_triggered=False,
            alert_message="", report={"answer": "x"},
        )
        assert results == []

    async def test_channel_inherits_job_then_console(self, jobs, subs, notifier):
        job = await _job(jobs, alert_channel="webhook:https://x.test/hook")
        await subs.create(job.id, "bob")
        results = await subs.deliver_for_run(
            job, run_id=1, status="ok", verdict="", alert_triggered=False,
            alert_message="", report={"answer": "x"},
        )
        assert results[0]["channel"] == "webhook:https://x.test/hook"

        # 订阅自己的通道优先于任务通道
        override = await subs.create(job.id, "carol", channel="console")
        assert override is not None
        results = await subs.deliver_for_run(
            job, run_id=2, status="ok", verdict="", alert_triggered=False,
            alert_message="", report={"answer": "x"},
        )
        by_sub = {r["subscriber"]: r for r in results}
        assert by_sub["carol"]["channel"] == "console"
        assert by_sub["bob"]["channel"] == "webhook:https://x.test/hook"

    async def test_bad_channel_recorded_failed_not_skipped(self, jobs, subs, notifier):
        job = await _job(jobs)
        await subs.create(job.id, "bob", channel="webhook:not-a-url")
        results = await subs.deliver_for_run(
            job, run_id=1, status="ok", verdict="", alert_triggered=False,
            alert_message="", report={"answer": "x"},
        )
        assert results[0]["status"] == "failed"
        assert "unsupported channel" in results[0]["error"]
        # 静默跳过会让「订了却从没收到」看起来像没生效；留痕才对得上账
        rows = await subs.list_deliveries()
        assert len(rows) == 1 and rows[0]["status"] == "failed"

    async def test_channel_failure_and_exception_never_raise(self, jobs, subs, monkeypatch):
        job = await _job(jobs)
        await subs.create(job.id, "bob")

        rec = RecordingNotifier(ok=False)
        monkeypatch.setattr(subscribe_mod, "build_notifier", lambda ch: rec)
        results = await subs.deliver_for_run(
            job, run_id=1, status="ok", verdict="", alert_triggered=False,
            alert_message="", report={"answer": "x"},
        )
        assert results[0]["error"] == "channel reported failure"

        boom = RecordingNotifier(error=RuntimeError("boom-" + "x" * 500))
        monkeypatch.setattr(subscribe_mod, "build_notifier", lambda ch: boom)
        results = await subs.deliver_for_run(
            job, run_id=2, status="ok", verdict="", alert_triggered=False,
            alert_message="", report={"answer": "x"},
        )
        assert results[0]["status"] == "failed"
        assert results[0]["error"].startswith("boom-")
        assert len(results[0]["error"]) <= 200

    async def test_redelivery_same_run_keeps_first_record(self, jobs, subs, notifier):
        job = await _job(jobs)
        await subs.create(job.id, "bob")
        await subs.deliver_for_run(
            job, run_id=1, status="ok", verdict="", alert_triggered=False,
            alert_message="", report={"answer": "first"},
        )
        await subs.deliver_for_run(
            job, run_id=1, status="ok", verdict="", alert_triggered=False,
            alert_message="", report={"answer": "second"},
        )
        rows = await subs.list_deliveries()
        assert len(rows) == 1
        assert rows[0]["excerpt"] == "first"

    async def test_driver_line_appended_to_message(self, jobs, subs, notifier):
        job = await _job(jobs)
        await subs.create(job.id, "bob")
        await subs.deliver_for_run(
            job, run_id=1, status="ok", verdict="", alert_triggered=False,
            alert_message="", report={
                "answer": "本期贷款余额下滑",
                "driver": "region=华东（贡献 62.0%）",
            },
        )
        message = notifier.sent[0]["message"]
        assert message.startswith("本期贷款余额下滑")
        assert "主因：region=华东（贡献 62.0%）" in message


# ── runner delivery (fake session manager) ──────────────


class FakeSessionManager:
    def __init__(self, finals, raise_on_ask: Exception | None = None):
        self._finals = list(finals)
        self._raise = raise_on_ask
        self.asked: list[str] = []

    async def start_session(self):
        return object()

    async def ask(self, session, question, workflow, datasource=None):
        self.asked.append(question)
        if self._raise is not None:
            raise self._raise
        return self._finals.pop(0) if self._finals else self._finals[-1]

    async def resume(self, session, decision, workflow):
        return self._finals.pop(0) if self._finals else self._finals[-1]


def _final_state(**overrides):
    from trove.workflow.state import WorkflowState

    defaults = {
        "session_id": "s1", "question": "q",
        "columns": ["region", "amount"], "rows": [["east", 1200]],
        "row_count": 1, "verdict": "OK",
        "final_response": "本月贷款总量 120 万元",
    }
    defaults.update(overrides)
    return WorkflowState(**defaults)


class TestRunnerDelivery:
    async def test_report_record_and_delivery(self, jobs, subs, notifier):
        job = await _job(jobs)
        sub = await subs.create(job.id, "bob")
        runner = SchedulerRunner(FakeSessionManager([_final_state(
            sql="SELECT SUM(amount) FROM loan",
            analysis={"top_components": [
                {"dim": "region", "value": "华东", "contribution": 0.62,
                 "source": "dimension"}]},
        )]), jobs, subscriptions=subs)
        summary = await runner.run_job(job)
        assert summary["status"] == "ok"

        # 报告落进 run.result_json —— 定时分析的产出物
        recent = await jobs.store.recent_run(job.id)
        report = recent["result"]
        assert report["answer"] == "本月贷款总量 120 万元"
        assert report["sql"].startswith("SELECT SUM")
        assert report["driver"] == "region=华东（贡献 62.0%）"
        assert report["analysis"] is True
        assert report["datasource"] == "demo"

        # 同一份报告投给了订阅者，主因行随消息
        assert len(notifier.sent) == 1
        payload = notifier.sent[0]
        assert payload["subscriber"] == "bob"
        assert payload["status"] == "ok"
        assert "主因：region=华东" in payload["message"]
        rows = await subs.list_deliveries(subscription_id=sub.id)
        assert len(rows) == 1 and rows[0]["run_id"] == recent["id"]

    async def test_error_run_still_delivers(self, jobs, subs, notifier):
        """日报静默消失比一条失败通知更糟 —— error 也要投。"""
        job = await _job(jobs)
        await subs.create(job.id, "bob")
        runner = SchedulerRunner(
            FakeSessionManager([_final_state(error="boom", final_response="")]),
            jobs, subscriptions=subs)
        summary = await runner.run_job(job)
        assert summary["status"] == "error"
        assert len(notifier.sent) == 1
        assert notifier.sent[0]["report"]["error"] == "boom"

    async def test_crash_still_delivers(self, jobs, subs, notifier):
        job = await _job(jobs)
        await subs.create(job.id, "bob")
        runner = SchedulerRunner(
            FakeSessionManager([], raise_on_ask=RuntimeError("kaboom")),
            jobs, subscriptions=subs)
        summary = await runner.run_job(job)
        assert summary["status"] == "error"
        assert len(notifier.sent) == 1
        assert "kaboom" in notifier.sent[0]["report"]["error"]

    async def test_broken_delivery_never_turns_run_error(self, jobs, subs, monkeypatch):
        """投递子系统炸了也不能改运行结果 —— best-effort 的最后一道。"""
        job = await _job(jobs)
        await subs.create(job.id, "bob")

        class Exploding:
            async def deliver_for_run(self, *a, **kw):
                raise RuntimeError("delivery subsystem down")

        runner = SchedulerRunner(FakeSessionManager([_final_state()]), jobs,
                                 subscriptions=Exploding())
        summary = await runner.run_job(job)
        assert summary["status"] == "ok"

    async def test_no_subscriptions_wired_is_noop(self, jobs):
        job = await _job(jobs)
        runner = SchedulerRunner(FakeSessionManager([_final_state()]), jobs)
        summary = await runner.run_job(job)
        assert summary["status"] == "ok"

    async def test_decision_run_delivers_rule_report(self, jobs, subs, notifier):
        """决策路径同样投递：报告 = 规则消息（含主因行）+ 计数。"""
        from types import SimpleNamespace

        from trove.services.decision.rules import DecisionRule
        from trove.services.decision.service import DecisionOutcome

        rule = DecisionRule(id="loan-drop", name="贷款下滑",
                            conditions=["delta_pct < -0.1"])
        evidence = {
            "rule_id": "loan-drop",
            "evidence": {"sql_current": "SELECT ...", "row_count": 2},
            "analysis": {"top_components": [
                {"dim": "region", "value": "华东", "contribution": 0.62,
                 "source": "dimension"}]},
        }

        class FakeDecision:
            def __init__(self):
                self.calls = []
                self.kb = SimpleNamespace(load_decisions=lambda ds: SimpleNamespace(
                    rules=[rule], digest="sha256:deadbeef"))

            async def evaluate(self, r, datasource, now=None, *, rule_digest=""):
                self.calls.append((r.id, datasource, rule_digest))
                return DecisionOutcome(
                    triggered=True, message="[warning] 贷款下滑 — 华东: -20%",
                    rule_id="loan-drop", evidence=evidence)

        job = await _job(jobs, decision_rule="loan-drop")
        await subs.create(job.id, "bob")
        manager = FakeSessionManager([])
        runner = SchedulerRunner(manager, jobs, decision=FakeDecision(),
                                 subscriptions=subs)
        summary = await runner.run_job(job)
        assert summary["status"] == "alert"
        assert manager.asked == []  # 决策路径绝不进 NL 管线

        assert len(notifier.sent) == 1
        payload = notifier.sent[0]
        assert payload["report"]["rule_id"] == "loan-drop"
        assert payload["report"]["row_count"] == 2
        assert "主因：region=华东" in payload["message"]

    async def test_missing_row_count_and_empty_answer_bounded(self, jobs, subs, notifier):
        job = await _job(jobs)
        await subs.create(job.id, "bob")
        long_answer = "长" * 5000
        runner = SchedulerRunner(FakeSessionManager([_final_state(
            final_response=long_answer, sql="S" * 3000)]), jobs, subscriptions=subs)
        await runner.run_job(job)
        report = (await jobs.store.recent_run(job.id))["result"]
        assert len(report["answer"]) == 4000
        assert len(report["sql"]) == 2000
