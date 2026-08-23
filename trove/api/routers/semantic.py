"""Semantic layer management endpoints (admin-only, draft approval flow).

Single source of truth stays the datasource's KB ``semantics.yml``; every
mutation goes through a pending draft that an admin confirms (applies to
YAML + re-syncs the mirror) or rejects. Every mutation writes an audit
entry (action, actor, status) like the rest of the admin surface.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import require_admin
from trove.api.schemas import SemanticDraftCreate

router = APIRouter()


def _kb(request: Request):
    return request.app.state.kb


def _registry(request: Request):
    return request.app.state.connector_registry


def _manager(request: Request):
    from trove.services.semantic_layer.manage import SemanticManager
    return SemanticManager(_kb(request))


async def _dialect(request: Request, name: str) -> str:
    """数据源 adapter 方言;未连接/异常 → sqlite 兜底(仅影响表达式校验)。"""
    try:
        adapter = await _registry(request).get(name)
        return adapter.dialect() or "sqlite"
    except Exception:
        return "sqlite"


async def _audit(request: Request, action: str, user: dict, status: int,
                 details: dict[str, Any] | None = None) -> None:
    await request.app.state.auth.record_audit(
        action, user=user, method=request.method, path=request.url.path,
        status=status, details=details,
    )


def _resolve_datasource(request: Request, name: str) -> str:
    if not _registry(request).is_registered(name):
        raise HTTPException(status_code=404, detail="datasource not found")
    return name


@router.get("/admin/semantic/{name}")
async def semantic_detail(
    name: str, request: Request, admin: dict = Depends(require_admin),
) -> dict:
    """One datasource's semantic model + lint issues + draft queue."""
    ds = _resolve_datasource(request, name)
    await _kb(request).ensure_synced(ds)
    return {"semantic": await _manager(request).detail(ds, dialect=await _dialect(request, ds))}


@router.post("/admin/semantic/{name}/drafts", status_code=201)
async def create_semantic_draft(
    name: str, body: SemanticDraftCreate, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """Create a pending draft (semantic_drafts.yml). semantics.yml untouched."""
    ds = _resolve_datasource(request, name)
    try:
        draft = await _manager(request).create_draft(
            ds, body.kind, body.action, body.name, body.payload or None, body.note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "semantic.draft.create", admin, 201, {
        "datasource": ds, "kind": body.kind, "action": body.action, "name": body.name,
    })
    return {"draft": draft}


@router.post("/admin/semantic/{name}/drafts/{draft_id}/confirm")
async def confirm_semantic_draft(
    name: str, draft_id: str, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """Approve: apply the draft to semantics.yml, mark applied, re-sync."""
    ds = _resolve_datasource(request, name)
    try:
        draft = await _manager(request).confirm_draft(
            ds, draft_id, dialect=await _dialect(request, ds))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "semantic.draft.confirm", admin, 200, {
        "datasource": ds, "id": draft_id, "kind": draft["kind"], "name": draft["name"],
    })
    return {"draft": draft}


@router.post("/admin/semantic/{name}/drafts/{draft_id}/reject")
async def reject_semantic_draft(
    name: str, draft_id: str, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """Reject: mark rejected (semantics.yml untouched)."""
    ds = _resolve_datasource(request, name)
    try:
        draft = await _manager(request).reject_draft(ds, draft_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "semantic.draft.reject", admin, 200, {
        "datasource": ds, "id": draft_id, "kind": draft["kind"], "name": draft["name"],
    })
    return {"draft": draft}
