"""JobsService — scheduling + alerting orchestration (deterministic bookkeeping).

Pure orchestration: the service computes due jobs, records runs, evaluates
alert rules, and dispatches notifiers. Actual question execution lives in
the CLI runner (it owns the LLM/session machinery); this module stays
independently testable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from trove.core.logging import get_logger
from trove.services.jobs.alerts import evaluate_alert
from trove.services.jobs.cron import cron_next, interval_next
from trove.services.jobs.notify import Notifier, build_notifier
from trove.services.jobs.store import Job, JobStore, Run

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def compute_next_run(schedule_type: str, schedule: str, after: datetime | None = None) -> str:
    """Next run timestamp (ISO) for a job's schedule; '' when invalid."""
    now = (after or _utcnow()).replace(second=0, microsecond=0)
    if schedule_type == "cron":
        nxt = cron_next(schedule, now)
    else:
        try:
            nxt = interval_next(max(1, int(schedule)), now)
        except (ValueError, TypeError):
            nxt = None
    return nxt.isoformat() if nxt else ""


class JobsService:
    def __init__(self, store: JobStore):
        self.store = store

    # ── job lifecycle ────────────────────────────────────

    async def create_job(
        self,
        question: str,
        schedule: str,
        schedule_type: str = "interval",
        *,
        name: str = "",
        datasource: str = "demo",
        workflow: str = "reflection",
        alert_expr: str = "",
        alert_channel: str = "",
        alert_cooldown_min: int = 30,
        decision_rule: str = "",
        topic: str = "",
        scan_spec: str = "",
    ) -> Job | None:
        # A decision/scan job carries `question` only as a human label — the
        # run never asks it. Still required, so the jobs list stays readable.
        if not question.strip():
            return None
        if schedule_type not in ("interval", "cron"):
            return None
        next_run = compute_next_run(schedule_type, schedule)
        if not next_run:
            return None
        job = Job(
            name=name.strip() or question.strip()[:24],
            question=question.strip(),
            schedule=schedule.strip(),
            schedule_type=schedule_type,
            datasource=datasource or "demo",
            workflow=workflow or "reflection",
            alert_expr=alert_expr.strip(),
            alert_channel=alert_channel.strip(),
            alert_cooldown_min=alert_cooldown_min,
            decision_rule=decision_rule.strip(),
            topic=topic.strip(),
            scan_spec=scan_spec.strip(),
            next_run_at=next_run,
        )
        await self.store.save_job(job)
        return job

    async def list_jobs(self) -> list[Job]:
        return await self.store.load_jobs()

    async def get_job(self, job_id: str) -> Job | None:
        return await self.store.get_job(job_id)

    async def cancel(self, job_id: str) -> bool:
        return await self.store.delete_job(job_id)

    async def toggle(self, job_id: str, enabled: bool) -> Job | None:
        job = await self.store.get_job(job_id)
        if job is None:
            return None
        job.enabled = enabled
        job.next_run_at = compute_next_run(job.schedule_type, job.schedule) if enabled else ""
        await self.store.save_job(job)
        return job

    async def update_job(
        self,
        job_id: str,
        *,
        name: str | None = None,
        question: str | None = None,
        schedule: str | None = None,
        schedule_type: str | None = None,
        datasource: str | None = None,
        workflow: str | None = None,
        alert_expr: str | None = None,
        alert_channel: str | None = None,
        alert_cooldown_min: int | None = None,
        decision_rule: str | None = None,
        topic: str | None = None,
        scan_spec: str | None = None,
        enabled: bool | None = None,
    ) -> Job | None:
        """Update mutable job fields (None = unchanged).

        Schedule changes recompute next_run_at when the job is enabled;
        a new schedule that does not parse leaves the job unchanged and
        returns None.
        """
        job = await self.store.get_job(job_id)
        if job is None:
            return None
        st = schedule_type or job.schedule_type
        if st not in ("interval", "cron"):
            return None
        sc = schedule or job.schedule
        # 新调度须可解析(即使 job 停用也要校验,否则下次启用拿不到 run time)。
        if not compute_next_run(st, sc):
            return None
        if name is not None:
            job.name = name.strip() or job.name
        if question is not None:
            job.question = question.strip()
        if schedule is not None:
            job.schedule = schedule.strip()
        if schedule_type is not None:
            job.schedule_type = st
        if datasource is not None:
            job.datasource = datasource
        if workflow is not None:
            job.workflow = workflow or "reflection"
        if alert_expr is not None:
            job.alert_expr = alert_expr.strip()
        if alert_channel is not None:
            job.alert_channel = alert_channel.strip()
        if alert_cooldown_min is not None:
            job.alert_cooldown_min = max(0, int(alert_cooldown_min))
        if decision_rule is not None:
            job.decision_rule = decision_rule.strip()
        if topic is not None:
            job.topic = topic.strip()
        if scan_spec is not None:
            job.scan_spec = scan_spec.strip()
        if enabled is not None:
            job.enabled = bool(enabled)
        job.next_run_at = compute_next_run(job.schedule_type, job.schedule) if job.enabled else ""
        await self.store.save_job(job)
        return job

    # ── scheduling ticks ─────────────────────────────────

    async def due_jobs(self, now: datetime | None = None) -> list[Job]:
        """Enabled jobs whose next_run_at has passed and needs a tick."""
        now = now or _utcnow()
        jobs: list[Job] = []
        for job in await self.store.load_jobs():
            if not job.enabled or not job.next_run_at:
                continue
            try:
                due = datetime.fromisoformat(job.next_run_at)
            except ValueError:
                continue
            if due <= now:
                jobs.append(job)
        return jobs

    async def advance(self, job: Job, now: datetime | None = None) -> str:
        """Advance a job to its next run time and persist."""
        job.next_run_at = compute_next_run(
            job.schedule_type, job.schedule, after=now or _utcnow(),
        )
        await self.store.save_job(job)
        return job.next_run_at

    async def record_run(self, job: Job, run: Run) -> int:
        return await self.store.start_run(run)

    async def finish_run(
        self, run_id: int, status: str, alert_triggered: bool,
        alert_sent: bool, row_count: int | None, verdict: str,
        result_json: dict[str, Any] | None = None,
    ) -> None:
        await self.store.finish_run(
            run_id, status, alert_triggered, alert_sent, row_count, verdict,
            result_json,
        )

    # ── alerting ─────────────────────────────────────────

    async def evaluate(self, job: Job, state: dict[str, Any]) -> dict[str, Any]:
        """Assess one run result against the job's alert rule.

        Returns {"triggered", "message", "notify": bool} — ``notify`` is
        False inside the cooldown window (alert dedup).
        """
        if not job.alert_expr:
            return {"triggered": False, "message": "", "notify": False}
        ver = evaluate_alert(
            job.alert_expr,
            columns=state.get("columns"),
            rows=state.get("rows"),
            row_count=state.get("row_count") or 0,
            verdict=state.get("verdict") or "",
        )
        return await self.evaluate_outcome(job, ver.triggered, ver.message)

    async def evaluate_outcome(
        self, job: Job, triggered: bool, message: str,
    ) -> dict[str, Any]:
        """Apply dedup to an already-decided verdict.

        Split out from :meth:`evaluate` because the decision path judges a
        run with the decision engine, not with ``job.alert_expr`` — calling
        ``evaluate`` there would re-run the (typically empty) threshold
        expression and throw the verdict away. Cooldown is the only part of
        the alerting contract that is the same for both paths, so it lives
        here and both callers share it.
        """
        notify = triggered and not await self._in_cooldown(job)
        return {"triggered": triggered, "message": message, "notify": notify}

    async def _in_cooldown(self, job: Job) -> bool:
        # `last_alert_run`, not `recent_run`: the caller is itself inside a
        # run whose row already exists (record_run happens first), so the
        # newest row of any kind is always the in-flight one — asking for it
        # makes this always False and the dedup a no-op.
        recent = await self.store.last_alert_run(job.id)
        if not recent or not recent.get("finished_at"):
            return False
        try:
            last = datetime.fromisoformat(recent["finished_at"])
        except (ValueError, TypeError):
            return False
        window_min = max(0, job.alert_cooldown_min)
        return (_utcnow() - last).total_seconds() < window_min * 60

    async def dispatch(self, job: Job, message: str, payload: dict[str, Any]) -> bool:
        """Send an alert through the job channel; False when unsendable."""
        if job.alert_channel:
            notifier: Notifier | None = build_notifier(job.alert_channel)
        else:
            # Default: console (always safe, best-effort)
            notifier = build_notifier("console")
        if notifier is None:
            return False
        body = {
            "job_id": job.id,
            "job_name": job.name,
            "question": job.question,
            "expr": job.alert_expr,
            # A decision job has no threshold expression — the rule id is what
            # tells the receiver which rule fired. Empty for ordinary jobs.
            "decision_rule": job.decision_rule,
            "message": message,
            "payload": payload,
        }
        try:
            await notifier.send(body)
        except Exception as e:  # notification failure never blocks the tick
            logger.warning("[ALERT] dispatch failed for %s: %s", job.id, e)
            return False
        return True