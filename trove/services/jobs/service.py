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
    ) -> Job | None:
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
    ) -> None:
        await self.store.finish_run(
            run_id, status, alert_triggered, alert_sent, row_count, verdict,
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
        notify = ver.triggered and not await self._in_cooldown(job)
        return {"triggered": ver.triggered, "message": ver.message, "notify": notify}

    async def _in_cooldown(self, job: Job) -> bool:
        recent = await self.store.recent_run(job.id)
        if not recent or not recent.get("alert_triggered") or not recent.get("finished_at"):
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
            "message": message,
            "payload": payload,
        }
        try:
            await notifier.send(body)
        except Exception as e:  # notification failure never blocks the tick
            logger.warning("[ALERT] dispatch failed for %s: %s", job.id, e)
            return False
        return True