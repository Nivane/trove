"""Scheduled-jobs endpoints (admin) — create/manage/trace scheduled questions.

``POST /v1/admin/jobs`` registers a scheduled question with a cron/interval
schedule and an optional threshold alert; the serve lifespan's background
tick executes due jobs through the same pipeline as interactive chat
(auto-approved, read-only). Every mutation writes an audit entry.

The job store is keyed by id only (admin-managed surface); run history is
exposed per job for the management UI.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from trove.api.deps import require_admin, require_admin_or_analyst
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
        # Non-empty → `question` is a label only and `workflow`/`alert_expr`
        # do not apply: the run is decided by the decision engine.
        "decision_rule": job.decision_rule,
        # 主题域(空 = 不限定):NL 路径把问题收敛到该域。
        "topic": job.topic,
        # 主动扫描规格(解析后的 dict;空 dict = 非扫描任务)。写侧一律
        # 存 JSON 字符串(列是 TEXT),读侧解析回去 —— 前端拿到的是形状,
        # 不是一个需要二次解析的字符串。
        "scan_spec": _scan_spec_out(job.scan_spec),
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


def _rule_error(request: Request, datasource: str, rule_id: str) -> str | None:
    """400 message for an unusable decision rule reference; None when fine.

    Checked here rather than in JobsService so the service needs no KB: the
    router already holds ``app.state.kb``. A dangling reference has to be
    rejected at write time — the runner would otherwise discover it on every
    tick and fail the same way forever.
    """
    from trove.services.decision.rules import RuleError

    rule_id = (rule_id or "").strip()
    if not rule_id:
        return None
    kb = getattr(request.app.state, "kb", None)
    if kb is None:
        return f"decision rules unavailable: no KB for datasource {datasource!r}"
    try:
        doc = kb.load_decisions(datasource)
    except RuleError as e:
        return f"decisions.yml for {datasource!r} is invalid: {e}"
    ids = [r.id for r in doc.rules]
    if rule_id not in ids:
        known = ", ".join(ids) or "(none declared)"
        return f"unknown decision_rule {rule_id!r} for {datasource!r} (declared: {known})"
    rule = next(r for r in doc.rules if r.id == rule_id)
    if not rule.enabled:
        return f"decision_rule {rule_id!r} is disabled"
    return None


def _scan_spec_out(raw: str) -> dict[str, Any]:
    """TEXT 列 → dict。坏 JSON 返回空 dict(写侧已校验,此处只是防御)。"""
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _scan_spec_in(spec: dict[str, Any] | None) -> str:
    """dict → TEXT 列。空 dict/None = 非扫描任务(空串)。"""
    if not spec:
        return ""
    return json.dumps(spec, ensure_ascii=False)


def _scan_error(request: Request, datasource: str, spec: dict[str, Any]) -> str | None:
    """400 message for an unusable scan spec; None when fine (or not a scan).

    Same discipline as ``_rule_error``/``_topic_error``, and it matters more
    here: a scan spec names metrics and dimensions *inside* one datasource's
    semantic model, so a typo is a cross-run dangling reference — the job
    would tick forever producing nothing but ``unverifiable`` rows. The
    reference check therefore runs at **write time, with zero queries**: it
    resolves names against the model only.
    """
    if not spec:
        return None
    from trove.services.scan.models import ScanError, ScanSpec
    from trove.services.scan.scanner import spec_issues
    from trove.services.semantic_layer.manage import SemanticManager

    try:
        parsed = ScanSpec.from_dict(spec)
    except ScanError as e:
        return f"invalid scan_spec: {e}"
    kb = getattr(request.app.state, "kb", None)
    if kb is None:
        return f"scan unavailable: no KB for datasource {datasource!r}"
    model = SemanticManager(kb).model(datasource)
    if model is None:
        return f"no semantic model for datasource: {datasource}"
    issues = spec_issues(_ModelLayer(model), parsed)
    if issues:
        return "scan_spec references are not usable: " + "; ".join(issues)
    return None


class _ModelLayer:
    """``resolve_*`` 只要求 ``.model()`` —— 校验侧不需要 provider 的其余面。"""

    def __init__(self, model: Any) -> None:
        self._model = model

    def model(self) -> Any:
        return self._model


def _topic_error(request: Request, datasource: str, topic: str) -> str | None:
    """400 message for an unusable topic reference; None when fine.

    Same discipline as ``_rule_error`` (checked at write time — the runner
    would fail the same way on every tick, forever), but the judgement itself
    lives in ``semantic_layer.manage.topic_reference_error`` because the CLI
    needs the identical check and the two must not drift apart.
    """
    from trove.services.semantic_layer.manage import topic_reference_error

    return topic_reference_error(
        getattr(request.app.state, "kb", None), datasource, topic)


@router.get("/admin/jobs")
async def list_jobs(
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    admin: dict = Depends(require_admin_or_analyst),
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
    rule_err = _rule_error(request, body.datasource, body.decision_rule)
    if rule_err:
        raise HTTPException(status_code=400, detail=rule_err)
    topic_err = _topic_error(request, body.datasource, body.topic)
    if topic_err:
        raise HTTPException(status_code=400, detail=topic_err)
    scan_err = _scan_error(request, body.datasource, body.scan_spec)
    if scan_err:
        raise HTTPException(status_code=400, detail=scan_err)
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
        decision_rule=body.decision_rule,
        topic=body.topic,
        scan_spec=_scan_spec_in(body.scan_spec),
    )
    if job is None:
        raise HTTPException(status_code=400, detail="invalid job definition")
    await _audit(request, "jobs.create", admin, 201, {"id": job.id, "name": job.name})
    return {"job": _serialize(job)}


@router.get("/admin/jobs/{job_id}")
async def get_job(
    job_id: str, request: Request, admin: dict = Depends(require_admin_or_analyst),
) -> dict:
    job = await _job_or_404(request, job_id)
    return {"job": _serialize(job)}


@router.patch("/admin/jobs/{job_id}")
async def update_job(
    job_id: str, body: JobPatch, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    existing = await _job_or_404(request, job_id)
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
    if body.decision_rule is not None:
        # Validate against the datasource the job will *have*, so editing
        # datasource and rule in one PATCH is judged as the end state.
        rule_err = _rule_error(
            request, body.datasource or existing.datasource, body.decision_rule)
        if rule_err:
            raise HTTPException(status_code=400, detail=rule_err)
    if body.topic is not None:
        # Same end-state rule as decision_rule above: a PATCH that moves the
        # job to another datasource *and* sets a topic judges the pair it
        # will actually have.
        topic_err = _topic_error(
            request, body.datasource or existing.datasource, body.topic)
        if topic_err:
            raise HTTPException(status_code=400, detail=topic_err)
    if body.scan_spec is not None:
        # End-state judgement again: moving the job to another datasource and
        # setting a scan spec in one PATCH judges the pair it will have.
        scan_err = _scan_error(
            request, body.datasource or existing.datasource, body.scan_spec)
        if scan_err:
            raise HTTPException(status_code=400, detail=scan_err)
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
        decision_rule=body.decision_rule,
        topic=body.topic,
        scan_spec=None if body.scan_spec is None else _scan_spec_in(body.scan_spec),
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
    admin: dict = Depends(require_admin_or_analyst),
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
