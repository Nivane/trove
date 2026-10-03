"""Decision jobs — the runner's fork, and the schedule guarantee around it.

The decision path shares the job store and the dispatch/cooldown machinery
with the NL path but shares nothing else: it is evaluated by
``DecisionService`` against the semantic model, with zero LLM calls. Two
properties matter here and neither is visible from the decision tests:

1. **The NL pipeline is never entered.** A decision job's ``question`` is a
   human label. If it leaked into ``session_manager.ask`` the job would run
   an LLM question and store a verdict about *that* answer.
2. **``advance`` runs even when the run fails.** A job whose ``next_run_at``
   never moves stays due, so every tick re-runs it and re-sends the alert —
   an accidental notification loop.
3. **The outbound hooks (verdict history, action proposals) are best-effort.**
   They run after the judgment, and a failure in either is a log line — the
   schedule ran and the verdict is stored, so neither may turn that into an
   error (P2/P3).
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from trove.services.decision.rules import ActionRef, DecisionRule
from trove.services.decision.service import DecisionOutcome
from trove.services.jobs.runner import SchedulerRunner
from trove.services.jobs.service import JobsService
from trove.services.jobs.store import JobStore

NOW = datetime(2026, 9, 27, 10, 0, 0)
RULE = DecisionRule(id="loan-drop", name="贷款余额环比下滑",
                    conditions=["delta_pct < -0.1"])

EVIDENCE = {
    "rule_id": "loan-drop",
    "rows": [{"dim": "华东", "current": 1600.0, "triggered": True}],
    "evidence": {"sql_current": "SELECT ...", "row_count": 2},
    "provenance": {"datasource": "demo"},
}


class FakeSessionManager:
    """Records every ask — the assertion is that there are none."""

    def __init__(self):
        self.asked = []

    async def start_session(self):
        return object()

    async def ask(self, session, question, workflow, datasource=None):
        self.asked.append(question)
        raise AssertionError("a decision job must not ask the NL pipeline")

    async def resume(self, session, decision, workflow):
        raise AssertionError("a decision job must not resume the NL pipeline")


class FakeDoc:
    def __init__(self, rules, digest="sha256:deadbeef"):
        self.rules = list(rules)
        self.digest = digest


class FakeKb:
    """The runner reads the whole document, not just the rule — the digest it
    records as "which version judged this" has to come from the same read."""

    def __init__(self, rule=RULE, digest="sha256:deadbeef"):
        self.doc = FakeDoc([] if rule is None else [rule], digest)
        self.asked: list[str] = []

    def load_decisions(self, datasource):
        self.asked.append(datasource)
        return self.doc


class FakeDecision:
    """Duck-typed DecisionService: records calls, returns a scripted outcome."""

    def __init__(self, outcome, kb=None):
        self.outcome = outcome
        self.kb = kb if kb is not None else FakeKb()
        self.calls: list[tuple] = []

    async def evaluate(self, rule, datasource, now=None, *, rule_digest=""):
        self.calls.append((rule, datasource, now, rule_digest))
        return self.outcome


ANALYSIS = {
    "top_components": [{"dim": "region", "value": "华东", "delta": -20.0,
                        "contribution": 0.62, "source": "dimension"}],
    "tree": None,
    "residual": {"value": 0.0, "exact": True, "reason": "identity"},
    "queries": [{"id": 1, "purpose": "driver_dimension", "sql": "SELECT ..."}],
    "degraded": [],
}


def _triggered(**overrides):
    base = {"triggered": True, "message": "[warning] 贷款余额环比下滑 — 华东: -20%",
            "rule_id": "loan-drop", "evidence": EVIDENCE}
    base.update(overrides)
    return DecisionOutcome(**base)


class FakeVerdicts:
    """Duck-typed ``VerdictStore``: records what it is given, or explodes."""

    def __init__(self, fail: bool = False):
        self.records: list = []
        self.fail = fail

    async def record(self, verdict):
        if self.fail:
            raise RuntimeError("verdict store on fire")
        self.records.append(verdict)
        return len(self.records)


class FakeActions:
    """Duck-typed ``ActionService``: records the propose calls, or explodes.

    The *policy* (disabled layer / notify_only / dedup) belongs to the real
    service and is tested there; here the only question is whether the runner
    hands it the right firing, and whether a refusal can touch the schedule.
    """

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    async def propose_from_verdict(self, *, rule, outcome, datasource,
                                   job_id="", run_id=None,
                                   created_by="system", evidence_refs=None):
        if self.fail:
            raise ValueError("template 'notify-ops' is 'pending', not confirmed")
        self.calls.append({
            "rule": rule, "outcome": outcome, "datasource": datasource,
            "job_id": job_id, "run_id": run_id, "created_by": created_by,
            "evidence_refs": evidence_refs,
        })
        return SimpleNamespace(id="p-1", status="pending")


@pytest.fixture
async def svc(tmp_path):
    service = JobsService(JobStore(tmp_path))
    yield service
    await service.store.dispose()


async def _job(svc, **overrides):
    kwargs = {"question": "贷款余额环比", "schedule": "5",
              "decision_rule": "loan-drop", "alert_channel": "console"}
    kwargs.update(overrides)
    return await svc.create_job(kwargs.pop("question"), kwargs.pop("schedule"),
                                "interval", **kwargs)


class TestRunJobForks:
    async def test_never_asks_the_nl_pipeline(self, svc):
        job = await _job(svc)
        manager = FakeSessionManager()          # raises if asked
        decision = FakeDecision(_triggered())
        runner = SchedulerRunner(manager, svc, decision=decision)

        summary = await runner.run_job(job, NOW)

        assert manager.asked == []
        assert summary["status"] == "alert"
        assert summary["rule_id"] == "loan-drop"
        assert decision.calls and decision.calls[0][1] == "demo"

    async def test_datasource_selects_the_rule(self, svc):
        """The rule is looked up in the *job's* datasource, not a default —
        otherwise a job would be judged against another datasource's model."""
        job = await _job(svc, datasource="financial")
        decision = FakeDecision(_triggered())
        runner = SchedulerRunner(FakeSessionManager(), svc, decision=decision)
        await runner.run_job(job, NOW)
        assert decision.kb.asked == ["financial"]

    async def test_a_normal_job_still_goes_to_the_pipeline(self, svc):
        """The fork is opt-in: an empty decision_rule keeps the old path."""
        job = await svc.create_job("q", "5", "interval")
        manager = FakeSessionManager()
        manager.ask = _stub_ask(manager)
        runner = SchedulerRunner(manager, svc, decision=FakeDecision(_triggered()))
        await runner.run_job(job, NOW)
        assert manager.asked == ["q"]

    async def test_missing_decision_service_is_an_error_not_a_fallback(self, svc):
        """Running the label question through the LLM would produce a
        plausible verdict about the wrong thing — refuse instead."""
        job = await _job(svc)
        runner = SchedulerRunner(FakeSessionManager(), svc, decision=None)
        summary = await runner.run_job(job, NOW)
        assert summary["status"] == "error"
        assert "decision service" in summary["error"] or summary["error"]


def _stub_ask(manager):
    async def ask(session, question, workflow, datasource=None):
        manager.asked.append(question)
        from trove.workflow.state import WorkflowState

        return WorkflowState(session_id="s1", question=question,
                             columns=["a"], rows=[["1"]], row_count=1,
                             verdict="OK")
    return ask


class TestEvidenceIsStored:
    async def test_the_run_row_carries_the_evidence(self, svc):
        """A decision run has no LangGraph trace, so the run record is the
        only thing that can answer "which SQL, which window, which rule"."""
        job = await _job(svc)
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 decision=FakeDecision(_triggered()))
        await runner.run_job(job, NOW)

        runs = await svc.store.list_runs(job.id)
        assert runs[0]["status"] == "alert"
        assert runs[0]["verdict"] == "loan-drop"
        assert runs[0]["result"]["evidence"]["sql_current"] == "SELECT ..."
        assert runs[0]["result"]["rows"][0]["dim"] == "华东"

    async def test_the_rule_digest_is_handed_to_the_engine(self, svc):
        """"This fired on the 10% threshold" has to stay answerable after the
        threshold is edited — and for a decision run the evidence is the only
        place that can say which version of the file judged it."""
        job = await _job(svc)
        decision = FakeDecision(_triggered())
        runner = SchedulerRunner(FakeSessionManager(), svc, decision=decision)
        await runner.run_job(job, NOW)

        assert decision.kb.asked == ["demo"]
        assert decision.calls[0][3] == "sha256:deadbeef"

    async def test_unserializable_evidence_does_not_strand_the_schedule(self, svc):
        """Postgres returns Decimal/datetime. If storing evidence raised, the
        job would never advance and would re-alert on every tick."""
        from decimal import Decimal

        job = await _job(svc)
        runner = SchedulerRunner(FakeSessionManager(), svc, decision=FakeDecision(
            _triggered(evidence={"rule_id": "loan-drop",
                                 "amount": Decimal("12.5")})))
        before = job.next_run_at
        await runner.run_job(job, NOW)

        runs = await svc.store.list_runs(job.id)
        assert runs[0]["status"] == "alert"
        # `default=str` stringifies rather than coercing — it is the last-ditch
        # net, not the normalizer. DecisionService._jsonable is what turns a
        # Decimal into a float on the way in; this only has to not raise.
        assert runs[0]["result"]["amount"] == "12.5"
        reloaded = await svc.get_job(job.id)
        assert reloaded.next_run_at != before


class TestVerdictHistory:
    """The verdict history is the decision layer's audit line (P2)."""

    async def test_the_verdict_is_recorded_with_run_identity(self, svc):
        """A verdict without its run identity is a floating claim: the run
        row and the verdict have to point at each other."""
        job = await _job(svc)
        store = FakeVerdicts()
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 decision=FakeDecision(_triggered()),
                                 verdicts=store)
        await runner.run_job(job, NOW)

        assert len(store.records) == 1
        v = store.records[0]
        assert v.rule_id == "loan-drop" and v.status == "alert"
        assert v.triggered is True and v.datasource == "demo"
        assert v.job_id == job.id
        assert v.run_id == (await svc.store.list_runs(job.id))[0]["id"]

    async def test_untriggered_runs_are_recorded_too(self, svc):
        """"Nothing wrong today" is a verdict — a history of only alerts
        cannot tell "checked, quiet" from "never ran"."""
        job = await _job(svc)
        store = FakeVerdicts()
        runner = SchedulerRunner(FakeSessionManager(), svc, decision=FakeDecision(
            _triggered(triggered=False, message="")), verdicts=store)
        await runner.run_job(job, NOW)

        assert len(store.records) == 1
        assert store.records[0].status == "ok"
        assert store.records[0].triggered is False

    async def test_analysis_evidence_survives_into_the_verdict(self, svc):
        """The bridge summary is stored verbatim: the diff view later reads
        the cards, and the audit has to be able to re-read the SQL."""
        job = await _job(svc)
        store = FakeVerdicts()
        runner = SchedulerRunner(FakeSessionManager(), svc, decision=FakeDecision(
            _triggered(evidence={**EVIDENCE, "analysis": ANALYSIS})),
            verdicts=store)
        await runner.run_job(job, NOW)

        analysis = store.records[0].evidence["analysis"]
        assert analysis["top_components"][0]["value"] == "华东"
        assert analysis["queries"][0]["sql"] == "SELECT ..."

    async def test_a_failing_verdict_store_never_breaks_the_schedule(self, svc):
        """Same discipline as `advance`: the run judged fine, so the run is
        fine — a history that cannot be written is logged, not escalated."""
        job = await _job(svc)
        before = job.next_run_at
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 decision=FakeDecision(_triggered()),
                                 verdicts=FakeVerdicts(fail=True))
        summary = await runner.run_job(job, NOW)

        assert summary["status"] == "alert" and summary["alert_sent"] is True
        runs = await svc.store.list_runs(job.id)
        assert runs[0]["status"] == "alert"
        assert (await svc.get_job(job.id)).next_run_at != before


class TestActionProposals:
    """A fired rule with ``autonomy: propose`` → a pending proposal (P3).

    The runner's job is narrow: hand the *firing* to the service, and let
    nothing it does come back as a run error. Dedup, the template gate and
    the disabled switch are the service's (see ``tests/services/action/``).
    """

    @staticmethod
    def _proposing_rule(**kw):
        base = dict(id="loan-drop", name="贷款余额环比下滑",
                    conditions=["delta_pct < -0.1"],
                    recommendation="联系运营核对额度",
                    action=ActionRef(template="notify-ops",
                                     autonomy="propose"))
        base.update(kw)
        return DecisionRule(**base)

    async def test_a_fired_propose_rule_reaches_the_service(self, svc):
        job = await _job(svc)
        actions = FakeActions()
        runner = SchedulerRunner(
            FakeSessionManager(), svc, decision=FakeDecision(
                _triggered(), kb=FakeKb(self._proposing_rule())),
            verdicts=FakeVerdicts(), actions=actions)
        await runner.run_job(job, NOW)

        assert len(actions.calls) == 1
        call = actions.calls[0]
        assert call["rule"].id == "loan-drop"
        assert call["outcome"].triggered is True
        assert call["datasource"] == "demo"
        assert call["job_id"] == job.id
        assert call["created_by"] == "system"
        # run identity + the exact evidence row the verdict was stored as
        assert call["run_id"] == (await svc.store.list_runs(job.id))[0]["id"]
        assert call["evidence_refs"] == {"verdict_id": 1}

    async def test_a_refusal_from_the_service_stays_a_log_line(self, svc):
        """The run judged fine and the verdict is stored — an unconfirmed
        template must not turn that into an error, and must not stall the
        schedule (same discipline as ``_advance``)."""
        job = await _job(svc)
        before = job.next_run_at
        runner = SchedulerRunner(
            FakeSessionManager(), svc, decision=FakeDecision(
                _triggered(), kb=FakeKb(self._proposing_rule())),
            actions=FakeActions(fail=True))
        summary = await runner.run_job(job, NOW)

        assert summary["status"] == "alert"
        assert summary["alert_sent"] is True
        assert (await svc.get_job(job.id)).next_run_at != before

    async def test_notify_only_rules_never_reach_the_service(self, svc):
        """``notify_only`` means the alert *is* the response — the action
        layer is not even consulted (and the service's policy agrees)."""
        job = await _job(svc)
        actions = FakeActions()
        rule = self._proposing_rule(
            action=ActionRef(template="notify-ops", autonomy="notify_only"))
        runner = SchedulerRunner(
            FakeSessionManager(), svc,
            decision=FakeDecision(_triggered(), kb=FakeKb(rule)),
            actions=actions)
        await runner.run_job(job, NOW)
        assert actions.calls == []

    async def test_quiet_or_erroring_runs_never_propose(self, svc):
        """A proposal is a request for a human's time — "nothing wrong" and
        "could not judge" are both reasons not to make one."""
        job = await _job(svc)
        quiet = FakeActions()
        runner = SchedulerRunner(
            FakeSessionManager(), svc, decision=FakeDecision(
                _triggered(triggered=False, message=""),
                kb=FakeKb(self._proposing_rule())),
            actions=quiet)
        await runner.run_job(job, NOW)
        assert quiet.calls == []

        broken = FakeActions()
        runner = SchedulerRunner(
            FakeSessionManager(), svc, decision=FakeDecision(
                _triggered(triggered=False, message="", error="no model"),
                kb=FakeKb(self._proposing_rule())),
            actions=broken)
        await runner.run_job(job, NOW)
        assert broken.calls == []

    async def test_no_action_layer_wired_is_a_noop(self, svc):
        """A deployment without P3 (``actions=None``) runs exactly as before."""
        job = await _job(svc)
        runner = SchedulerRunner(
            FakeSessionManager(), svc, decision=FakeDecision(
                _triggered(), kb=FakeKb(self._proposing_rule())))
        summary = await runner.run_job(job, NOW)
        assert summary["status"] == "alert"


class TestDriverLineInTheMessage:
    """补丁 1 的最后一公里:通知里多一行「主因：…」."""

    async def test_the_notification_gains_the_driver_line(self, svc, monkeypatch):
        job = await _job(svc)
        sent: list[str] = []

        async def fake_dispatch(job, message, state):
            sent.append(message)
            return True

        monkeypatch.setattr(svc, "dispatch", fake_dispatch)
        runner = SchedulerRunner(FakeSessionManager(), svc, decision=FakeDecision(
            _triggered(evidence={**EVIDENCE, "analysis": ANALYSIS})))
        summary = await runner.run_job(job, NOW)

        assert sent and sent[0] == summary["alert"]
        assert summary["alert"].endswith("主因：region=华东（贡献 62.0%）")
        assert summary["alert"].startswith("[warning] 贷款余额环比下滑")

    async def test_without_analysis_the_message_is_untouched(self, svc):
        """Single-leaf metric / bridge not applicable: no line, not an empty
        「主因：—」 line."""
        job = await _job(svc)
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 decision=FakeDecision(_triggered()))
        summary = await runner.run_job(job, NOW)
        assert summary["alert"] == _triggered().message
        assert "主因" not in summary["alert"]

    async def test_an_empty_component_list_adds_nothing(self, svc):
        """A bridge that ran but found nothing to say (all deltas missing)
        is the same silence as one that never ran."""
        job = await _job(svc)
        runner = SchedulerRunner(FakeSessionManager(), svc, decision=FakeDecision(
            _triggered(evidence={**EVIDENCE,
                                 "analysis": {**ANALYSIS, "top_components": []}})))
        summary = await runner.run_job(job, NOW)
        assert "主因" not in summary["alert"]


class TestErrorsAreLoud:
    async def test_rule_missing_is_error_and_sends_nothing(self, svc):
        job = await _job(svc, decision_rule="gone")
        decision = FakeDecision(_triggered(), kb=FakeKb(rule=None))
        runner = SchedulerRunner(FakeSessionManager(), svc, decision=decision)
        summary = await runner.run_job(job, NOW)

        assert summary["status"] == "error"
        assert "gone" in summary["error"]
        assert summary["alert_sent"] is False
        assert decision.calls == []          # never evaluated against nothing

        runs = await svc.store.list_runs(job.id)
        assert runs[0]["status"] == "error"
        assert runs[0]["alert_triggered"] is False

    async def test_disabled_rule_is_error(self, svc):
        job = await _job(svc)
        disabled = DecisionRule(id="loan-drop", enabled=False,
                                conditions=["current > 0"])
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 decision=FakeDecision(_triggered(),
                                                       kb=FakeKb(disabled)))
        summary = await runner.run_job(job, NOW)
        assert summary["status"] == "error"
        assert "disabled" in summary["error"]

    async def test_engine_reported_error_is_recorded_not_alerted(self, svc):
        """`error` is how the decision engine says "could not judge" — the
        one outcome that must never be dressed up as ok or as an alert."""
        job = await _job(svc)
        runner = SchedulerRunner(FakeSessionManager(), svc, decision=FakeDecision(
            _triggered(triggered=False, message="", error="no semantic model")))
        summary = await runner.run_job(job, NOW)

        assert summary["status"] == "error"
        assert summary["error"] == "no semantic model"
        assert summary["alert"] == ""
        assert summary["alert_sent"] is False

    async def test_no_trigger_records_ok(self, svc):
        job = await _job(svc)
        runner = SchedulerRunner(FakeSessionManager(), svc, decision=FakeDecision(
            _triggered(triggered=False, message="")))
        summary = await runner.run_job(job, NOW)
        assert summary["status"] == "ok"
        assert summary["alert"] == ""


class TestScheduleAlwaysAdvances:
    async def test_advance_happens_on_the_error_path(self, svc):
        job = await _job(svc, decision_rule="gone")
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 decision=FakeDecision(_triggered(),
                                                       kb=FakeKb(rule=None)))
        before = job.next_run_at
        await runner.run_job(job, NOW)
        reloaded = await svc.get_job(job.id)
        assert reloaded.next_run_at != before

    async def test_advance_survives_a_failing_finish_run(self, svc, monkeypatch):
        """The amplifier this guard exists for: finish_run raising used to
        skip advance entirely, so the job stayed due forever."""

        async def boom(*a, **k):
            raise RuntimeError("db is on fire")

        job = await _job(svc)
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 decision=FakeDecision(_triggered()))
        monkeypatch.setattr(svc, "finish_run", boom)
        before = job.next_run_at

        summary = await runner.run_job(job, NOW)

        assert summary["status"] == "error"
        reloaded = await svc.get_job(job.id)
        assert reloaded.next_run_at != before

    async def test_advance_failure_does_not_escape(self, svc, monkeypatch):
        """Raising here would escape `tick` and take the scheduler loop down."""

        async def boom(*a, **k):
            raise RuntimeError("no clock")

        job = await _job(svc)
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 decision=FakeDecision(_triggered()))
        monkeypatch.setattr(svc, "advance", boom)
        summary = await runner.run_job(job, NOW)      # must not raise
        assert summary["status"] == "alert"


class TestCooldownIsShared:
    async def test_second_trigger_within_cooldown_is_not_sent(self, svc):
        job = await _job(svc, alert_cooldown_min=30)
        runner = SchedulerRunner(FakeSessionManager(), svc,
                                 decision=FakeDecision(_triggered()))

        first = await runner.run_job(job, NOW)
        assert first["alert_sent"] is True
        second = await runner.run_job(job, NOW)
        assert second["status"] == "alert"
        assert second["alert_sent"] is False
        assert second["alert"]           # still reported, just not re-sent

    async def test_a_bad_engine_outcome_is_not_re_sent_on_cooldown(self, svc):
        """Cooldown must not be entered by an error — otherwise a broken rule
        would suppress the first real alert that followed it."""
        job = await _job(svc, alert_cooldown_min=30)
        runner = SchedulerRunner(FakeSessionManager(), svc, decision=FakeDecision(
            _triggered(triggered=False, message="", error="boom")))
        await runner.run_job(job, NOW)

        runs = await svc.store.list_runs(job.id)
        assert runs[0]["alert_sent"] is False
        assert await svc._in_cooldown(job) is False


class TestStoreRoundTrip:
    async def test_decision_rule_survives_a_save_load(self, svc):
        job = await _job(svc, decision_rule="loan-drop")
        reloaded = await svc.get_job(job.id)
        assert reloaded.decision_rule == "loan-drop"

    async def test_default_is_empty_so_old_jobs_stay_nl_jobs(self, svc):
        job = await svc.create_job("q", "5", "interval")
        assert (await svc.get_job(job.id)).decision_rule == ""

    async def test_update_and_clear(self, svc):
        job = await _job(svc)
        await svc.update_job(job.id, decision_rule="")
        assert (await svc.get_job(job.id)).decision_rule == ""

    async def test_legacy_db_without_the_column_is_migrated(self, tmp_path):
        """`jobs` predates the migration framework, so the column is added by
        probe on open. A legacy DB must read back as NL jobs, not crash."""
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
                next_run_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO jobs VALUES ('legacy','n','old question','demo','reflection',
                'interval','5',1,'','',30,'','2026-01-01','2026-01-01');
        """)
        conn.commit()
        conn.close()

        svc = JobsService(JobStore(tmp_path))
        try:
            job = await svc.get_job("legacy")
            assert job is not None
            assert job.question == "old question"
            assert job.decision_rule == ""
            # ...and the column is now writable
            await svc.update_job("legacy", decision_rule="loan-drop")
            assert (await svc.get_job("legacy")).decision_rule == "loan-drop"
        finally:
            await svc.store.dispose()

    async def test_evidence_json_is_stored_raw(self, tmp_path):
        """`start_run` already serialized result_json; `finish_run` must write
        it too, since a decision run only learns its verdict at the end."""
        svc = JobsService(JobStore(tmp_path))
        try:
            job = await svc.create_job("q", "5", "interval")
            run_id = await svc.record_run(job, __import__(
                "trove.services.jobs.store", fromlist=["Run"]).Run(job_id=job.id))
            await svc.finish_run(run_id, "ok", False, False, 1, "OK",
                                 result_json={"a": 1})
            runs = await svc.store.list_runs(job.id)
            assert runs[0]["result"] == {"a": 1}
            assert json.loads(json.dumps(runs[0]["result"])) == {"a": 1}
        finally:
            await svc.store.dispose()
