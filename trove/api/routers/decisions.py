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

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import require_admin
from trove.api.schemas import DecisionDocBody
from trove.services.decision.rules import (
    RuleError,
    lint_document,
    parse_document,
)

router = APIRouter()


def _kb(request: Request):
    kb = getattr(request.app.state, "kb", None)
    if kb is None:
        raise HTTPException(status_code=409, detail="knowledge base not configured")
    return kb


def _jobs(request: Request):
    return getattr(request.app.state, "jobs", None)


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
    issues = lint_document(doc)
    if not doc.rules:
        # Display-only: an empty document is writable (it is also the only way
        # to delete the last rule), but a reader should know it means "nothing
        # is being watched", not "everything is fine".
        issues.insert(0, f"no decision rules declared for {datasource!r}")
    rules = []
    for rule in doc.rules:
        rules.append({
            "id": rule.id,
            "name": rule.name,
            "enabled": rule.enabled,
            "severity": rule.severity,
            "owner_role": rule.owner_role,
            "window": rule.window,
            "scope": rule.scope,
            "emit": rule.emit,
            "conditions": list(rule.conditions),
            "condition_mode": rule.condition_mode,
            "referenced_by": await _referencing_jobs(request, datasource, rule.id),
        })
    return {
        "datasource": datasource,
        "version": doc.version,
        "digest": doc.digest,
        "rules": rules,
        "issues": issues,
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
    try:
        doc = parse_document(body.model_dump())
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
