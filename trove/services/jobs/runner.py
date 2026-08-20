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
from trove.services.jobs.service import JobsService
from trove.services.jobs.store import Job, Run

logger = get_logger(__name__)

MAX_RESULT_ROWS = 200


class SchedulerRunner:
    def __init__(self, session_manager, jobs: JobsService, lang: str = "zh"):
        self.session_manager = session_manager
        self.jobs = jobs
        self.lang = lang

    async def run_job(self, job: Job, now: datetime | None = None) -> dict[str, Any]:
        """Execute one job end-to-end and return its run summary."""
        run = Run(job_id=job.id)
        run_id = await self.jobs.record_run(job, run)
        summary: dict[str, Any] = {"job_id": job.id, "name": job.name, "error": ""}
        try:
            session = await self.session_manager.start_session()
            try:
                final = await self.session_manager.ask(
                    session, job.question, job.workflow,
                )
                if getattr(final, "hitl_status", "") == "pending":
                    final = await self.session_manager.resume(
                        session, "approve", job.workflow,
                    )
            except Exception as e:
                logger.exception("job %s question failed", job.id)
                await self.jobs.finish_run(run_id, "error", False, False, 0, "")
                await self.jobs.advance(job, now)
                return {"job_id": job.id, "name": job.name, "error": str(e)[:200]}

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
            await self.jobs.advance(job, now)
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
            return {"job_id": job.id, "name": job.name, "error": str(e)[:200]}

    async def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Run all currently due jobs; returns per-job summaries."""
        due = await self.jobs.due_jobs(now)
        if not due:
            return []
        results = []
        for job in due:
            results.append(await self.run_job(job, now))
        return results