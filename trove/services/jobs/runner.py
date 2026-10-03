"""Scheduled-job runner — executes due jobs through the session manager.

Couples the deterministic JobsService bookkeeping with the live agent
pipeline. Scheduled runs are auto-approved (read-only execution channel),
so a job never blocks on a confirmation prompt.

Duck-typed against SessionManager so tests can drive it with a fake.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from trove.core.logging import get_logger
from trove.services.decision.bridge import primary_driver_line
from trove.services.decision.verdicts import verdict_from_outcome
from trove.services.jobs.service import JobsService
from trove.services.jobs.store import Job, Run

logger = get_logger(__name__)

MAX_RESULT_ROWS = 200


class SchedulerRunner:
    def __init__(self, session_manager, jobs: JobsService, lang: str = "zh",
                 decision=None, verdicts=None):
        self.session_manager = session_manager
        self.jobs = jobs
        self.lang = lang
        #: ``DecisionService`` (duck-typed) or None. Without it a decision job
        #: is reported as an error rather than run through the NL pipeline —
        #: answering the job's `question` label with the LLM would produce a
        #: plausible-looking verdict that has nothing to do with the rule.
        self.decision = decision
        #: ``VerdictStore`` (duck-typed) or None — the decision history. Both
        #: writes here are best-effort: a verdict store that is unavailable
        #: or failing must not turn a schedule that ran fine into an error.
        self.verdicts = verdicts

    async def run_job(self, job: Job, now: datetime | None = None) -> dict[str, Any]:
        """Execute one job end-to-end and return its run summary."""
        if job.decision_rule:
            return await self._run_decision(job, now)

        run = Run(job_id=job.id)
        run_id = await self.jobs.record_run(job, run)
        summary: dict[str, Any] = {
            "job_id": job.id, "name": job.name, "error": "", "status": "error",
            "row_count": 0, "alert": "", "alert_sent": False,
        }
        try:
            session = await self.session_manager.start_session()
            try:
                final = await self.session_manager.ask(
                    session, job.question, job.workflow,
                    datasource=job.datasource or None,
                )
                if getattr(final, "hitl_status", "") == "pending":
                    final = await self.session_manager.resume(
                        session, "approve", job.workflow,
                    )
            except Exception as e:
                logger.exception("job %s question failed", job.id)
                await self.jobs.finish_run(run_id, "error", False, False, 0, "")
                summary["error"] = str(e)[:200]
                return summary

            state = {
                "columns": list(getattr(final, "columns", [])),
                "rows": list(getattr(final, "rows", []))[:MAX_RESULT_ROWS],
                "row_count": int(getattr(final, "row_count", 0) or 0),
                "verdict": getattr(final, "verdict", ""),
            }
            error = getattr(final, "error", "")
            alert_eval = await self.jobs.evaluate(job, state)
            triggered = bool(alert_eval["triggered"])
            sent = False
            if alert_eval.get("notify"):
                sent = await self.jobs.dispatch(
                    job, alert_eval.get("message") or job.alert_expr,
                    {k: v for k, v in state.items() if k != "rows"},
                )
            status = "error" if error else ("alert" if triggered else "ok")
            row_count = state["row_count"] if not error else 0
            await self.jobs.finish_run(
                run_id, status, triggered, sent,
                row_count, state["verdict"],
            )
            summary.update({
                "status": status,
                "row_count": row_count,
                "error": error or "",
                "alert": alert_eval.get("message", "") if triggered else "",
                "alert_sent": sent,
            })
            return summary
        except Exception as e:
            logger.exception("job %s run crashed", job.id)
            try:
                await self.jobs.finish_run(run_id, "error", False, False, 0, "")
            except Exception:
                pass
            summary["error"] = str(e)[:200]
            return summary
        finally:
            # Unconditional on purpose: a job whose schedule never advances
            # stays due, so the next tick re-runs it and re-sends the alert —
            # forever. Anything that raises between here and `record_run`
            # (a Decimal in the evidence, a broken notifier) must not be able
            # to strand the schedule like that.
            await self._advance(job, now)

    async def _advance(self, job: Job, now: datetime | None) -> None:
        """Advance the job's next run time; a failure is logged, never raised
        (raising here would escape `tick` and take the scheduler loop down)."""
        try:
            await self.jobs.advance(job, now)
        except Exception:
            logger.exception("job %s failed to advance", job.id)

    async def _run_decision(self, job: Job, now: datetime | None) -> dict[str, Any]:
        """Run one decision job: fetch by rule, judge deterministically, store
        the evidence.

        The NL pipeline is never entered — a decision run must be replayable
        and must not depend on the model. `advance` is again unconditional,
        for the same reason as the NL path.
        """
        summary: dict[str, Any] = {
            "job_id": job.id, "name": job.name, "error": "", "status": "error",
            "row_count": 0, "alert": "", "alert_sent": False,
            "rule_id": job.decision_rule,
        }
        run = Run(job_id=job.id)
        run_id = await self.jobs.record_run(job, run)
        try:
            rule, rule_digest = await self._load_rule(job)
            outcome = await self.decision.evaluate(
                rule, job.datasource, now, rule_digest=rule_digest)
            # The decision engine never writes a business DB and never raises
            # out here: a failure comes back as `error` and is recorded as a
            # run status, which is the entire audit trail for this path.
            triggered = bool(outcome.triggered)
            error = outcome.error or ""
            message = self._notify_message(outcome)
            sent = False
            if error:
                status = "error"
            else:
                status = "alert" if triggered else "ok"
                verdict = await self.jobs.evaluate_outcome(job, triggered, message)
                sent = False
                if verdict["notify"]:
                    sent = await self.jobs.dispatch(
                        job, message,
                        {"rule_id": job.decision_rule,
                         "evidence": outcome.evidence},
                    )
            rows = outcome.evidence.get("evidence", {}).get("row_count") or 0
            await self.jobs.finish_run(
                run_id, status, triggered, sent,
                0 if error else int(rows), job.decision_rule,
                result_json=outcome.evidence,
            )
            await self._record_verdict(job, outcome, run_id)
            summary.update({
                "status": status,
                "row_count": 0 if error else int(rows),
                "error": error,
                "alert": message if triggered else "",
                "alert_sent": sent,
            })
            return summary
        except Exception as e:
            logger.exception("decision job %s crashed", job.id)
            try:
                await self.jobs.finish_run(run_id, "error", False, False, 0,
                                           job.decision_rule)
            except Exception:
                pass
            summary["error"] = str(e)[:200]
            return summary
        finally:
            await self._advance(job, now)

    def _notify_message(self, outcome: Any) -> str:
        """The rule's message plus the bridge's 「主因」 line, when it has one.

        Composed here rather than inside the decision service so the service
        keeps returning exactly what the rule produced — the evidence is the
        record, the message is the notification. Skipped whenever the bridge
        had nothing to say (single-leaf metric, no time field, all component
        deltas unreported): an empty 「主因：—」 line is worse than no line.
        """
        message = str(getattr(outcome, "message", "") or "")
        evidence = getattr(outcome, "evidence", None) or {}
        driver = primary_driver_line(evidence.get("analysis"))
        if not driver:
            return message
        return f"{message}\n主因：{driver}" if message else f"主因：{driver}"

    async def _record_verdict(self, job: Job, outcome: Any, run_id: int) -> None:
        """Best-effort append to the verdict history (the admin UI's audit line).

        The run row is the schedule's record; the verdict is the decision's.
        A store that is absent (feature not wired) or failing must never turn
        a schedule that judged fine into an error — log loudly, never raise.
        Same discipline as ``_advance``.
        """
        if self.verdicts is None:
            return
        try:
            verdict = verdict_from_outcome(
                outcome,
                datasource=job.datasource or "",
                job_id=job.id,
                run_id=run_id,
                now=datetime.now().isoformat(timespec="seconds"),
            )
            await self.verdicts.record(verdict)
        except Exception:
            logger.exception("decision job %s: verdict store write failed",
                             job.id)

    async def _load_rule(self, job: Job) -> tuple[Any, str]:
        """``(rule, document digest)`` for ``job.decision_rule``.

        A dangling reference is a hard error, not a skip: the job was created
        against a rule that has since been renamed or deleted, and reporting
        "nothing wrong today" for it would be the worst possible outcome.

        The digest comes from the same read as the rule — a decision run has
        no trace behind it, so the run record is the whole audit trail, and
        "which version of the rule judged this" is half of what it must
        answer. Reading the document directly (rather than ``get_decision``,
        which loads it again) keeps the two from disagreeing.
        """
        kb = getattr(self.decision, "kb", None)
        if kb is None:
            raise RuntimeError("decision service has no KB to load rules from")
        doc = kb.load_decisions(job.datasource)
        rule = next((r for r in doc.rules if r.id == job.decision_rule), None)
        if rule is None:
            raise RuntimeError(
                f"decision rule {job.decision_rule!r} not found for datasource "
                f"{job.datasource!r}")
        if not rule.enabled:
            raise RuntimeError(f"decision rule {job.decision_rule!r} is disabled")
        return rule, doc.digest

    async def run_job_now(self, job_id: str) -> dict[str, Any] | None:
        """Run a job immediately (manual trigger), regardless of schedule.

        Returns the run summary, or None when the job does not exist.
        """
        job = await self.jobs.get_job(job_id)
        if job is None:
            return None
        return await self.run_job(job)

    async def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Run all currently due jobs; returns per-job summaries."""
        due = await self.jobs.due_jobs(now)
        if not due:
            return []
        results = []
        for job in due:
            results.append(await self.run_job(job, now))
        return results