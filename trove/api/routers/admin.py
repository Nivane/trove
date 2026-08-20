"""Admin console endpoints — every route requires admin role.

Users / tokens / datasource grants / audit log / cross-user session view /
per-lesson KB approval. Every mutation writes an audit entry
(action, actor, status) so the management surface is traceable.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from trove.api.deps import require_admin
from trove.api.schemas import (
    DatasourcesPut,
    TokenCreate,
    UserCreate,
    UserPatch,
)
from trove.core.errors import AuthError

router = APIRouter()


def _auth(request: Request):
    return request.app.state.auth


async def _audit(request: Request, action: str, user: dict, status: int,
                 details: dict | None = None) -> None:
    await _auth(request).record_audit(
        action, user=user, method=request.method, path=request.url.path,
        status=status, details=details,
    )


async def _user_or_404(request: Request, user_id: int) -> dict:
    user = await _auth(request).store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"user not found: {user_id}")
    return user


@router.get("/admin/users")
async def list_users(request: Request, admin: dict = Depends(require_admin)) -> dict:
    return {"users": await _auth(request).list_users()}


@router.post("/admin/users", status_code=201)
async def create_user(
    body: UserCreate, request: Request, admin: dict = Depends(require_admin)
) -> dict:
    auth = _auth(request)
    try:
        user = await auth.create_user(
            body.username, body.password, role=body.role, display_name=body.display_name
        )
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "admin.user.create", admin, 201, {"username": user["username"]})
    return user


@router.patch("/admin/users/{user_id}")
async def update_user(
    user_id: int, body: UserPatch, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    auth = _auth(request)
    try:
        user = await auth.update_user(
            user_id, password=body.password, role=body.role,
            display_name=body.display_name, disabled=body.disabled,
        )
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if user is None:
        raise HTTPException(status_code=404, detail=f"user not found: {user_id}")
    await _audit(request, "admin.user.update", admin, 200, {"user_id": user_id})
    return user


@router.delete("/admin/users/{user_id}", status_code=204)
async def delete_user(
    user_id: int, request: Request, admin: dict = Depends(require_admin)
) -> None:
    auth = _auth(request)
    try:
        deleted = await auth.delete_user(user_id, actor_id=admin["id"])
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"user not found: {user_id}")
    await _audit(request, "admin.user.delete", admin, 204, {"user_id": user_id})


# ── Tokens ───────────────────────────────────────────────


@router.post("/admin/users/{user_id}/tokens", status_code=201)
async def create_token(
    user_id: int, body: TokenCreate, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    await _user_or_404(request, user_id)
    raw, record = await _auth(request).create_token(
        user_id, label=body.label, ttl_hours=body.ttl_hours
    )
    await _audit(request, "admin.token.create", admin, 201,
                 {"user_id": user_id, "label": body.label})
    return {"token": raw, "expires_at": record["expires_at"]}


@router.get("/admin/users/{user_id}/tokens")
async def list_tokens(
    user_id: int, request: Request, admin: dict = Depends(require_admin)
) -> dict:
    await _user_or_404(request, user_id)
    tokens = await _auth(request).list_tokens(user_id)
    # never leak token_hash (irreversible but still a secret artifact)
    return {"tokens": [
        {k: v for k, v in t.items() if k != "token_hash"} for t in tokens
    ]}


@router.delete("/admin/tokens/{token_id}", status_code=204)
async def revoke_token(
    token_id: int, request: Request, admin: dict = Depends(require_admin)
) -> None:
    if not await _auth(request).revoke_token(token_id):
        raise HTTPException(status_code=404, detail=f"token not found: {token_id}")
    await _audit(request, "admin.token.revoke", admin, 204, {"token_id": token_id})


# ── Datasource grants ────────────────────────────────────


@router.get("/admin/users/{user_id}/datasources")
async def get_datasources(
    user_id: int, request: Request, admin: dict = Depends(require_admin)
) -> dict:
    await _user_or_404(request, user_id)
    return {"datasources": await _auth(request).get_datasources(user_id)}


@router.put("/admin/users/{user_id}/datasources")
async def set_datasources(
    user_id: int, body: DatasourcesPut, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    await _user_or_404(request, user_id)
    await _auth(request).set_datasources(user_id, body.datasources)
    await _audit(request, "admin.grant.set", admin, 200,
                 {"user_id": user_id, "datasources": body.datasources})
    return {"datasources": body.datasources}


# ── Audit log ────────────────────────────────────────────


@router.get("/admin/audit")
async def list_audit(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user_id: int | None = None,
    action: str | None = None,
    admin: dict = Depends(require_admin),
) -> dict:
    entries = await _auth(request).list_audit(
        limit=limit, offset=offset, user_id=user_id, action=action
    )
    return {"audit": entries}


# ── Cross-user sessions (admin view) ─────────────────────


@router.get("/admin/sessions")
async def list_all_sessions(
    request: Request,
    user_id: str | None = None,
    admin: dict = Depends(require_admin),
) -> dict:
    """All sessions across users (optionally filtered by owner)."""
    manager = request.app.state.session_manager
    sessions = await manager.list_sessions(user_id=user_id)
    return {"sessions": sessions}


# ── KB lesson approval (per-lesson confirm/reject) ───────


def _kb(request: Request):
    return request.app.state.kb


def _kb_datasource(request: Request, datasource: str | None) -> str:
    ds = datasource or request.app.state.connector_registry.default_name
    if not ds:
        raise HTTPException(status_code=400, detail="no active datasource")
    return ds


@router.post("/admin/kb/lessons/{pattern}/confirm")
async def confirm_lesson(
    pattern: str, request: Request, datasource: str | None = None,
    admin: dict = Depends(require_admin),
) -> dict:
    ds = _kb_datasource(request, datasource)
    if not await _kb(request).confirm_lesson(ds, pattern):
        raise HTTPException(status_code=404, detail=f"lesson not found: {pattern}")
    await _audit(request, "kb.lesson.confirm", admin, 200,
                 {"datasource": ds, "pattern": pattern})
    return {"status": "ok", "pattern": pattern, "confirmed": True}


@router.post("/admin/kb/lessons/{pattern}/reject")
async def reject_lesson(
    pattern: str, request: Request, datasource: str | None = None,
    admin: dict = Depends(require_admin),
) -> dict:
    ds = _kb_datasource(request, datasource)
    if not await _kb(request).reject_lesson(ds, pattern):
        raise HTTPException(status_code=404, detail=f"lesson not found: {pattern}")
    await _audit(request, "kb.lesson.reject", admin, 200,
                 {"datasource": ds, "pattern": pattern})
    return {"status": "ok", "pattern": pattern, "rejected": True}
