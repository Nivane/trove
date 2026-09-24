"""Scheduled-jobs endpoints (admin) — create/manage/trace scheduled questions.

``POST /v1/admin/jobs`` registers a scheduled question with a cron/interval
schedule and an optional threshold alert; the serve lifespan's background
tick executes due jobs through the same pipeline as interactive chat
(auto-approved, read-only). Every mutation writes an audit entry.

The job store is keyed by id only (admin-managed surface); run history is
exposed per job for the management UI.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from trove.api.deps import require_admin
from trove.api.schemas import JobCreate, JobPatch
from trove.services.jobs.service import compute_next_run

router = APIRouter()

_SCHEDULE_TYPES = ("interval", "cron")


def _jobs(request: Request):
    jobs = getattr(request.app.state, "jobs", None)
    if jobs is None:
        raise HTTPException(status_code=409, detail="jobs not configured")
    return jobs


def _scheduler(request: Request):
    return getattr(request.app.state, "scheduler", None)


def _serialize(job) -> dict[str, Any]:
    recent = getattr(job, "_recent", None)
    return {
        "id": job.id,
        "name": job.name,
        "question": job.question,
        "datasource": job.datasource,
        "workflow": job.workflow,
        "schedule_type": job.schedule_type,
        "schedule": job.schedule,
        "enabled": bool(job.enabled),
        "alert_expr": job.alert_expr,
        "alert_channel": job.alert_channel,
        "alert_cooldown_min": job.alert_cooldown_min,
        "next_run_at": job.next_run_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "recent_run": recent,
    }


def _validate_schedule(schedule_type: str, schedule: str) -> str | None:
    """400 message for an invalid schedule; None when parseable."""
    if schedule_type not in _SCHEDULE_TYPES:
        return f"unsupported schedule_type: {schedule_type} (allowed: {', '.join(_SCHEDULE_TYPES)})"
    if not (schedule or "").strip():
        return "schedule is required"
    if not compute_next_run(schedule_type, schedule.strip()):
        return f"invalid {schedule_type} schedule: {schedule!r}"
    return None


async def _job_or_404(request: Request, job_id: str):
    job = await _jobs(request).get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return job


def _channel_error(channel: str) -> str | None:
    """Validate an alert channel string (console / webhook:<url>)."""
    from trove.services.jobs.notify import build_notifier

    channel = (channel or "").strip()
    if not channel:
        return None
    if build_notifier(channel) is None:
        return f"unsupported alert_channel: {channel} (allowed: console | webhook:<url>)"
    return None


@router.get("/admin/jobs")
async def list_jobs(
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    admin: dict = Depends(require_admin),
) -> dict:
    """All scheduled jobs, each with its most recent run status."""
    jobs = await _jobs(request).list_jobs()
    out = []
    for job in jobs[:limit]:
        job._recent = await _jobs(request).store.recent_run(job.id)
        out.append(_serialize(job))
    return {"jobs": out, "total": len(jobs)}


@router.post("/admin/jobs", status_code=201)
async def create_job(
    body: JobCreate, request: Request, admin: dict = Depends(require_admin),
) -> dict:
    schedule_err = _validate_schedule(body.schedule_type, body.schedule)
    if schedule_err:
        raise HTTPException(status_code=400, detail=schedule_err)
    channel_err = _channel_error(body.alert_channel)
    if channel_err:
        raise HTTPException(status_code=400, detail=channel_err)
    job = await _jobs(request).create_job(
        body.question,
        body.schedule.strip(),
        body.schedule_type,
        name=body.name,
        datasource=body.datasource,
        workflow=body.workflow,
        alert_expr=body.alert_expr,
        alert_channel=body.alert_channel,
        alert_cooldown_min=body.alert_cooldown_min,
    )
    if job is None:
        raise HTTPException(status_code=400, detail="invalid job definition")
    await _audit(request, "jobs.create", admin, 201, {"id": job.id, "name": job.name})
    return {"job": _serialize(job)}


@router.get("/admin/jobs/{job_id}")
async def get_job(
    job_id: str, request: Request, admin: dict = Depends(require_admin),
) -> dict:
    job = await _job_or_404(request, job_id)
    return {"job": _serialize(job)}


@router.patch("/admin/jobs/{job_id}")
async def update_job(
    job_id: str, body: JobPatch, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    await _job_or_404(request, job_id)
    if body.schedule is not None or body.schedule_type is not None:
        schedule_err = _validate_schedule(
            body.schedule_type or "interval", body.schedule or "1",
        )
        if schedule_err:
            raise HTTPException(status_code=400, detail=schedule_err)
    if body.alert_channel is not None:
        channel_err = _channel_error(body.alert_channel)
        if channel_err:
            raise HTTPException(status_code=400, detail=channel_err)
    job = await _jobs(request).update_job(
        job_id,
        name=body.name,
        question=body.question,
        schedule=body.schedule,
        schedule_type=body.schedule_type,
        datasource=body.datasource,
        workflow=body.workflow,
        alert_expr=body.alert_expr,
        alert_channel=body.alert_channel,
        alert_cooldown_min=body.alert_cooldown_min,
        enabled=body.enabled,
    )
    if job is None:
        raise HTTPException(status_code=400, detail="invalid job update")
    await _audit(request, "jobs.update", admin, 200, {"id": job_id})
    return {"job": _serialize(job)}


@router.post("/admin/jobs/{job_id}/run")
async def run_job_now(
    job_id: str, request: Request, admin: dict = Depends(require_admin),
) -> dict:
    """Run one job immediately (manual trigger), independent of schedule."""
    scheduler = _scheduler(request)
    if scheduler is None:
        raise HTTPException(status_code=409, detail="scheduler not configured")
    await _job_or_404(request, job_id)
    summary = await scheduler.run_job_now(job_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    await _audit(request, "jobs.run", admin, 200, {"id": job_id, "status": summary.get("status", "")})
    return {"run": summary}


@router.get("/admin/jobs/{job_id}/runs")
async def list_job_runs(
    job_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    admin: dict = Depends(require_admin),
) -> dict:
    await _job_or_404(request, job_id)
    runs = await _jobs(request).store.list_runs(job_id, limit=limit)
    return {"job_id": job_id, "runs": runs}


@router.delete("/admin/jobs/{job_id}", status_code=204)
async def delete_job(
    job_id: str, request: Request, admin: dict = Depends(require_admin),
) -> None:
    if not await _jobs(request).cancel(job_id):
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    await _audit(request, "jobs.delete", admin, 204, {"id": job_id})


async def _audit(request: Request, action: str, user: dict, status: int,
                 details: dict | None = None) -> None:
    auth = getattr(request.app.state, "auth", None)
    if auth is None or not hasattr(auth, "record_audit"):
        return
    try:
        await auth.record_audit(
            action, user=user, method=request.method, path=request.url.path,
            status=status, details=details,
        )
    except Exception:
        pass
