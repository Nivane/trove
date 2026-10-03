"""Decision-rule endpoints (admin) — read and edit ``decisions.yml``.

Rules are the declarative half of the decision layer: a rule names a metric
in the semantic model's vocabulary plus the condition under which it counts
as a problem, and a scheduled job evaluates it with zero LLM involvement.
They live inside the KB tree (``.trove/kb/<datasource>/decisions.yml``), so
every edit is a git commit and the digest returned here is what a run's
evidence records as "which version of the rule judged this".

This surface is deliberately read-heavy: the UI lists rules with their lint
issues and editor text, and PUT replaces the whole document at once. Partial
per-rule PATCH would need a merge story for concurrent edits to the same
file; replacing the document makes the write atomic and the git commit an
exact record of it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from trove.api.deps import require_admin
from trove.core.logging import get_logger
from trove.api.schemas import DecisionDocBody
from trove.services.decision.rules import (
    RuleError,
    lint_advisories,
    lint_document,
    lint_document_assets,
    parse_document,
)

router = APIRouter()
logger = get_logger(__name__)


def _kb(request: Request):
    kb = getattr(request.app.state, "kb", None)
    if kb is None:
        raise HTTPException(status_code=409, detail="knowledge base not configured")
    return kb


def _jobs(request: Request):
    return getattr(request.app.state, "jobs", None)


def _verdicts(request: Request):
    """The verdict store, or ``None`` when this process has none.

    ``None`` is not an error for the *list* view (rules still exist without a
    history — a fresh install, or ``trove serve`` wired without the store), so
    callers degrade to a null ``latest_verdict``. The two history endpoints
    need the store to answer at all, so they turn it into a 409.
    """
    return getattr(request.app.state, "verdicts", None)


def _verdict_brief(rec, diff: dict | None = None) -> dict[str, Any]:
    """A verdict without its evidence blob — what a history list renders.

    The evidence (SQL + raw rows) is the *detail* view's payload; inlining it
    per row would make one drawer-open cost a few hundred KB of JSON that the
    list never shows.
    """
    return {
        "id": rec.id,
        "datasource": rec.datasource,
        "rule_id": rec.rule_id,
        "rule_digest": rec.rule_digest,
        "run_id": rec.run_id,
        "job_id": rec.job_id,
        "status": rec.status,
        "triggered": rec.triggered,
        "severity": rec.severity,
        "priority": rec.priority,
        "message": rec.message,
        "error": rec.error,
        "row_count": rec.row_count,
        "evidence_truncated": rec.evidence_truncated,
        "anchor_date": rec.anchor_date,
        "evaluated_at": rec.evaluated_at,
        "created_at": rec.created_at,
        "diff": diff,
    }


def _asset_issues(request: Request, doc) -> list[str]:
    """Template-existence lint, i.e. the same closure ``save`` enforces.

    ``KbService`` injects the registry as a duck-typed object; here it is the
    app component. Same three-way semantics as ``KbService._asset_lint``:
    registry absent → unchecked (a process without the action layer must not
    report every rule as dangling); registry present → its two name sets. The
    list view is where an admin sees "this rule points at a template that was
    rejected/deleted" *before* the next fire, so it must not be quieter here
    than at save time.
    """
    templates = getattr(request.app.state, "action_templates", None)
    if templates is None:
        return []
    try:
        return lint_document_assets(
            doc, templates=set(templates.names()),
            confirmed=set(templates.confirmed_names()),
        )
    except Exception:  # 读模板目录失败:体检腿坏掉不该把规则列表也带走
        logger.warning("decision asset lint failed", exc_info=True)
        return []


async def _referencing_jobs(request: Request, datasource: str, rule_id: str) -> list[str]:
    """Job ids (name: id) that schedule this rule — deleting a rule that a job
    still points at would leave a job failing on every tick."""
    jobs = _jobs(request)
    if jobs is None:
        return []
    out = []
    for job in await jobs.list_jobs():
        if job.decision_rule == rule_id and job.datasource == datasource:
            out.append(f"{job.name} ({job.id})")
    return out


@router.get("/admin/decisions")
async def list_decisions(
    request: Request, datasource: str, admin: dict = Depends(require_admin),
) -> dict:
    """All rules for one datasource, with lint issues and job references."""
    kb = _kb(request)
    try:
        doc = kb.load_decisions(datasource)
    except RuleError as e:
        # A file that will not parse is not "no rules" — say so, or the UI
        # would offer to create a first rule and silently overwrite it.
        raise HTTPException(status_code=422, detail=str(e))
    issues = lint_document(doc) + _asset_issues(request, doc)
    if not doc.rules:
        # Display-only: an empty document is writable (it is also the only way
        # to delete the last rule), but a reader should know it means "nothing
        # is being watched", not "everything is fine".
        issues.insert(0, f"no decision rules declared for {datasource!r}")
    # One lookup for the whole page, and a failure here must not cost the
    # reader the rule list — "the history is unavailable" and "there is no
    # history" are different facts, so the former degrades to null per rule
    # and the list still renders.
    latest: dict[str, Any] = {}
    store = _verdicts(request)
    if store is not None and doc.rules:
        try:
            latest = await store.latest_for_rules(
                datasource, [r.id for r in doc.rules])
        except Exception:
            latest = {}
    rules = []
    for rule in doc.rules:
        last = latest.get(rule.id)
        rules.append({
            "id": rule.id,
            "name": rule.name,
            "enabled": rule.enabled,
            "severity": rule.severity,
            "priority": rule.priority,
            "recommendation": rule.recommendation,
            "owner_role": rule.owner_role,
            "window": rule.window,
            "scope": rule.scope,
            "emit": rule.emit,
            "conditions": list(rule.conditions),
            "condition_mode": rule.condition_mode,
            "action": (
                {"template": rule.action.template,
                 "autonomy": rule.action.autonomy,
                 "params": dict(rule.action.params)}
                if rule.action is not None else None
            ),
            "referenced_by": await _referencing_jobs(request, datasource, rule.id),
            "latest_verdict": _verdict_brief(last) if last is not None else None,
        })
    return {
        "datasource": datasource,
        "version": doc.version,
        "digest": doc.digest,
        "rules": rules,
        "issues": issues,
        # 提示级与拦截级**分开出**:advisory 从不拦保存,混进 issues 会让
        # 读者以为规则存不下去(与 save 的 422 判定不是同一张表)。
        "advisories": lint_advisories(doc),
    }


@router.get("/admin/decisions/raw")
async def get_decisions_raw(
    request: Request, datasource: str, admin: dict = Depends(require_admin),
) -> dict:
    """``decisions.yml`` as text, for the editor.

    Declared **before** ``/{rule_id}`` — Starlette matches routes in
    registration order, so the other way round this would be swallowed by the
    rule-id pattern and answer 404. Returns "" for a datasource with no file
    yet: an empty editor is the right starting point, and ``save`` creates it.
    """
    path = _kb(request).decisions_path(datasource)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return {"datasource": datasource, "text": text}


@router.get("/admin/decisions/verdicts/{verdict_id}")
async def get_verdict(
    verdict_id: int, request: Request, admin: dict = Depends(require_admin),
) -> dict:
    """One verdict with its full evidence, plus its diff against the previous.

    Declared **before** ``/{rule_id}`` (same Starlette ordering trap as
    ``/raw``): the paths do not actually overlap today — ``/{rule_id}`` is one
    segment — but a future ``/verdicts/...`` sibling declared below it would
    be shadowed, and the ordering is cheap insurance.

    The evidence carries raw business rows and is admin-only by design; a
    user-facing decision surface (later milestone) must re-mask on read
    rather than reuse this payload.
    """
    store = _verdicts(request)
    if store is None:
        raise HTTPException(
            status_code=409, detail="decision verdict store not configured")
    rec = await store.get(int(verdict_id))
    if rec is None:
        raise HTTPException(status_code=404,
                            detail=f"verdict not found: {verdict_id}")
    from dataclasses import asdict

    from trove.services.decision.verdicts import diff_verdicts

    prev = await store.previous_for(rec.datasource, rec.rule_id, rec)
    return {
        "verdict": asdict(rec),
        "diff": diff_verdicts(prev, rec) if prev is not None else None,
    }


@router.get("/admin/decisions/{rule_id}/verdicts")
async def list_verdicts(
    rule_id: str, request: Request, datasource: str,
    limit: int = Query(20, ge=1, le=200),
    since: str | None = Query(None),
    admin: dict = Depends(require_admin),
) -> dict:
    """Newest-first verdict history for one rule, each row diffed against the
    one before it (inline — the window is bounded, so no N+1).

    The oldest row of the returned window has ``diff: null``: its predecessor
    is outside the page. That is a boundary, not "nothing changed" — raise
    ``limit``/``since`` to reach further back.

    A rule with no history answers 200 with an empty list (it may simply have
    never been scheduled); only a missing *store* is a 409.
    """
    store = _verdicts(request)
    if store is None:
        raise HTTPException(
            status_code=409, detail="decision verdict store not configured")
    records = await store.list_for_rule(datasource, rule_id, limit=limit,
                                        since=since)
    from trove.services.decision.verdicts import diff_verdicts

    out = []
    for i, rec in enumerate(records):
        prev = records[i + 1] if i + 1 < len(records) else None
        out.append(_verdict_brief(
            rec, diff_verdicts(prev, rec) if prev is not None else None))
    return {
        "datasource": datasource,
        "rule_id": rule_id,
        "count": len(out),
        "verdicts": out,
    }


@router.get("/admin/decisions/{rule_id}")
async def get_decision(
    rule_id: str, request: Request, datasource: str,
    admin: dict = Depends(require_admin),
) -> dict:
    kb = _kb(request)
    try:
        doc = kb.load_decisions(datasource)
    except RuleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    rule = next((r for r in doc.rules if r.id == rule_id), None)
    if rule is None:
        raise HTTPException(
            status_code=404,
            detail=f"decision rule not found: {rule_id} (datasource {datasource!r})",
        )
    from trove.services.decision.rules import rule_to_dict

    return {
        "datasource": datasource,
        "digest": doc.digest,
        "rule": rule_to_dict(rule),
        "issues": [i for i in lint_document(doc) if rule_id in i],
        "referenced_by": await _referencing_jobs(request, datasource, rule_id),
    }


@router.put("/admin/decisions")
async def put_decisions(
    body: DecisionDocBody, request: Request, admin: dict = Depends(require_admin),
) -> dict:
    """Replace the whole ``decisions.yml`` for a datasource.

    ``KbService.save_decisions`` is the only write gate and it lints
    unconditionally — a rule that would never fire is refused rather than
    persisted as a job that silently does nothing. There is deliberately no
    bypass here: adding one would only move the enforcement, not remove it.
    """
    kb = _kb(request)
    if (body.rules is None) == (body.text is None):
        raise HTTPException(
            status_code=400,
            detail="send exactly one of 'rules' (structured) or 'text' (raw YAML)",
        )
    data: dict[str, Any] = body.model_dump()
    if body.text is not None:
        import yaml

        try:
            parsed = yaml.safe_load(body.text)
        except yaml.YAMLError as e:
            raise HTTPException(status_code=400, detail=f"invalid YAML: {e}")
        if parsed is not None and not isinstance(parsed, dict):
            # ``dict.update`` would raise TypeError on a list and surface as a
            # 500 — the editor is a free-text box, so this is a typo away.
            raise HTTPException(
                status_code=400,
                detail=f"decisions.yml must be a mapping, got {type(parsed).__name__}",
            )
        data.update(parsed or {})
    try:
        doc = parse_document(data)
    except RuleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    actor = str((admin or {}).get("username", ""))
    try:
        result = await kb.save_decisions(
            body.datasource, doc,
            message=body.message or "",
            trailers={"Generator": "decisions.save", "Approved-by": actor}
            if actor else None,
        )
    except RuleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "decisions.save", admin, 200, {
        "datasource": body.datasource, "rules": len(doc.rules),
    })
    return {
        "datasource": body.datasource,
        "rules": len(doc.rules),
        "git": result if isinstance(result, dict) else {},
    }


async def _audit(request: Request, action: str, user: dict, status: int,
                 details: dict[str, Any] | None = None) -> None:
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
