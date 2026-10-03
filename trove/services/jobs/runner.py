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
                 decision=None, verdicts=None, actions=None, subscriptions=None):
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
        #: ``ActionService`` (duck-typed) or None — turns a fired verdict into
        #: a pending proposal (P3). Best-effort for the same reason as the
        #: verdict store, and with one more of its own: a proposal is a
        #: *request for a human*, so a failure to create one must never take
        #: the schedule (or the verdict) down with it.
        self.actions = actions
        #: ``SubscriptionService`` (duck-typed) or None — 把本次 run 的报告
        #: 投递给订阅者（定时分析 + 订阅的投递半边）。同样 best-effort：一个
        #: 收不到的订阅者绝不能把一次跑好的运行变成 error。
        self.subscriptions = subscriptions

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
                # 失败也是订阅者要知道的事：日报静默消失比一条失败通知更糟。
                await self._deliver(
                    job, run_id, status="error", verdict="",
                    alert_triggered=False, alert_message="",
                    report={"question": job.question, "error": str(e)[:200]},
                )
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
            # 报告记录 = 定时分析的产出物：答案 markdown（分析型问题天然带上
            # 归因表/驱动树）+ 主因行 + SQL + 计数，逐字段有界截断。此前 NL
            # 运行不落任何结果，订阅投递与回看都无从谈起。
            report = self._report_payload(job, final)
            await self.jobs.finish_run(
                run_id, status, triggered, sent,
                row_count, state["verdict"], result_json=report,
            )
            await self._deliver(
                job, run_id, status=status, verdict=str(state["verdict"] or ""),
                alert_triggered=triggered,
                alert_message=alert_eval.get("message", "") if triggered else "",
                report=report,
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
            await self._deliver(
                job, run_id, status="error", verdict="",
                alert_triggered=False, alert_message="",
                report={"question": job.question, "error": str(e)[:200]},
            )
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
            verdict_id = await self._record_verdict(job, outcome, run_id)
            await self._propose_action(job, rule, outcome, run_id, verdict_id)
            # 订阅投递：判定 run 的报告 = 规则消息（含「主因」行）+ 计数。
            # 与 NL 路径的 answer 报告同构，收件方拿到的都是一份「本期发生了什么」。
            await self._deliver(
                job, run_id, status=status, verdict=job.decision_rule,
                alert_triggered=triggered,
                alert_message=message if triggered else "",
                report={
                    "question": job.question,
                    "rule_id": job.decision_rule,
                    "message": message,
                    "row_count": 0 if error else int(rows),
                    "error": error,
                },
            )
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
            await self._deliver(
                job, run_id, status="error", verdict=job.decision_rule,
                alert_triggered=False, alert_message="",
                report={"question": job.question,
                        "rule_id": job.decision_rule,
                        "error": str(e)[:200]},
            )
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

    def _report_payload(self, job: Job, final: Any) -> dict[str, Any]:
        """有界报告记录（写进 ``run.result_json``）——定时分析的产出物。

        答案 markdown 自带归因表/驱动树（分析型问题走归因管线），所以
        ``answer`` + ``driver`` 就是可投递、可回看的报告本体；逐字段显式
        截断，防止一次 run 把 jobs.sqlite 撑爆（result_json 是长文本列）。
        """
        analysis = getattr(final, "analysis", None)
        return {
            "question": job.question,
            "datasource": job.datasource,
            "answer": str(getattr(final, "final_response", "") or "")[:4000],
            "sql": str(getattr(final, "sql", "") or "")[:2000],
            "driver": primary_driver_line(analysis),
            "analysis": bool(analysis),
            "row_count": int(getattr(final, "row_count", 0) or 0),
            "verdict": str(getattr(final, "verdict", "") or ""),
            "error": str(getattr(final, "error", "") or "")[:200],
        }

    async def _deliver(
        self, job: Job, run_id: int, *, status: str, verdict: str,
        alert_triggered: bool, alert_message: str, report: dict[str, Any],
    ) -> None:
        """把本期报告投递给订阅者。best-effort —— 与 verdict/action 同级
        纪律：投递子系统缺席或失败只是日志，绝不改运行的结果。"""
        if self.subscriptions is None:
            return
        try:
            await self.subscriptions.deliver_for_run(
                job, run_id=run_id, status=status, verdict=verdict,
                alert_triggered=alert_triggered, alert_message=alert_message,
                report=report,
            )
        except Exception:
            logger.exception("job %s: subscription delivery failed", job.id)

    async def _record_verdict(
        self, job: Job, outcome: Any, run_id: int,
    ) -> int | None:
        """Best-effort append to the verdict history (the admin UI's audit line).

        The run row is the schedule's record; the verdict is the decision's.
        A store that is absent (feature not wired) or failing must never turn
        a schedule that judged fine into an error — log loudly, never raise.
        Same discipline as ``_advance``. Returns the new verdict id so the
        proposal can point back at the exact evidence row it came from.
        """
        if self.verdicts is None:
            return None
        try:
            verdict = verdict_from_outcome(
                outcome,
                datasource=job.datasource or "",
                job_id=job.id,
                run_id=run_id,
                now=datetime.now().isoformat(timespec="seconds"),
            )
            return int(await self.verdicts.record(verdict) or 0) or None
        except Exception:
            logger.exception("decision job %s: verdict store write failed",
                             job.id)
            return None

    async def _propose_action(
        self, job: Job, rule: Any, outcome: Any, run_id: int,
        verdict_id: int | None,
    ) -> None:
        """A fired rule with ``autonomy: propose`` → a pending proposal (P3).

        Best-effort exactly like ``_record_verdict``: the schedule ran and the
        verdict is stored, so a proposal failure is a log line, never an error
        on the run. The service owns the policy (disabled layer, no action,
        ``notify_only``, dedup); a ``ProposalError`` here means the rule points
        at a template that cannot produce a payload — loud in the log, and no
        half-built proposal (see ``ActionService.propose_from_verdict``).
        """
        if self.actions is None:
            return
        action = getattr(rule, "action", None)
        if action is None or action.autonomy != "propose":
            return
        if not getattr(outcome, "triggered", False) or getattr(outcome, "error", ""):
            return
        try:
            refs = {"verdict_id": verdict_id} if verdict_id else None
            await self.actions.propose_from_verdict(
                rule=rule, outcome=outcome, datasource=job.datasource or "",
                job_id=job.id, run_id=run_id, created_by="system",
                evidence_refs=refs,
            )
        except Exception:
            logger.exception(
                "decision job %s: action proposal failed for rule %s",
                job.id, getattr(rule, "id", "?"))

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