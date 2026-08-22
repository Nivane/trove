"""Admin console endpoints — every route requires admin role.

Users / tokens / datasource grants / audit log / cross-user session view /
per-lesson KB approval. Every mutation writes an audit entry
(action, actor, status) so the management surface is traceable.
"""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from trove.api.deps import require_admin
from trove.api.schemas import (
    DatasourcesPut,
    TokenCreate,
    UserCreate,
    UserPatch,
)
from trove.core.errors import AuthError, DatasourceError
from trove.core.types import DatasourceConfig
from trove.services.datasource.urls import parse_datasource_url

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


def _store(request: Request):
    return request.app.state.config_store


def _registry(request: Request):
    return request.app.state.connector_registry


def _sanitized(cfg: DatasourceConfig) -> dict:
    return {"name": cfg.name, "type": cfg.type, "default": bool(cfg.default)}


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


# ── Datasource management ────────────────────────────────


@router.get("/admin/datasources")
async def list_admin_datasources(request: Request, admin: dict = Depends(require_admin)) -> dict:
    registry = _registry(request)
    kb = _kb(request)
    # 幂等：KB 目录存在但镜像未建（如挂载的 .trove）时先建表，否则 list_items 500
    await kb.ensure_synced(None)
    out = []
    for info in registry.list_info():
        out.append({
            **info,
            "status": "connected",
            "kb_initialized": bool(kb.init_exists(info["name"])),
            "kb_items": (await kb.list_items()).get(info["name"], {}),
        })
    registered = {d["name"] for d in out}
    for cfg in _store(request).load_configs():
        if cfg.name not in registered:
            out.append({
                **_sanitized(cfg), "type": cfg.type,
                "status": "disconnected", "kb_initialized": False, "kb_items": {},
            })
    return {"datasources": out}


@router.post("/admin/datasources", status_code=201)
async def create_datasource(request: Request, body: dict,
                            admin: dict = Depends(require_admin)) -> dict:
    registry = _registry(request)
    store = _store(request)
    name = (body.get("name") or "").strip()
    url = (body.get("url") or "").strip()
    if not name and not url:
        raise HTTPException(status_code=400, detail="name or url required")
    try:
        if url == "demo" or (name == "demo" and not url):
            from trove.services.datasource.demo_setup import setup_demo_datasource
            # 先取当前默认态再注册：无默认时新源成为默认（注册顺序即默认语义），
            # 持久化的 default 必须与注册结果一致，否则重启后 demo 不会被恢复为默认。
            was_default = registry.default_name is None
            await setup_demo_datasource(registry, set_default=was_default)
            cfg = DatasourceConfig(name="demo", type="demo",
                                   connection_params={}, credentials={},
                                   default=was_default)
        else:
            cfg = parse_datasource_url(url)
            if name:
                cfg = dataclasses.replace(cfg, name=name)  # DatasourceConfig 是 dataclass，无 model_copy
            # 新注册源在无默认时成为默认（default 持久化，重启按 datasources.yml 恢复）；
            # 默认的管理 API（改默认/取消默认）属范围外（spec §6）。
            await registry.register(cfg, set_default=cfg.default or registry.default_name is None)
    except DatasourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    configs = store.load_configs()
    configs = [c for c in configs if c.name != cfg.name] + [cfg]
    store.save_configs(configs)
    await _audit(request, "datasource.create", admin, 201, {"name": cfg.name, "type": cfg.type})
    return {"datasource": _sanitized(cfg)}


@router.delete("/admin/datasources/{name}", status_code=204)
async def delete_datasource(name: str, request: Request,
                            admin: dict = Depends(require_admin)) -> None:
    registry = _registry(request)
    store = _store(request)
    if registry.is_registered(name):
        await registry.unregister(name)
    store.save_configs([c for c in store.load_configs() if c.name != name])
    await _audit(request, "datasource.delete", admin, 204, {"name": name})


@router.post("/admin/datasources/{name}/reconnect")
async def reconnect_datasource(name: str, request: Request,
                               admin: dict = Depends(require_admin)) -> dict:
    registry = _registry(request)
    store = _store(request)
    cfg = next((c for c in store.load_configs() if c.name == name), None)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"datasource not found: {name}")
    if registry.is_registered(name):
        await registry.unregister(name)
    try:
        if cfg.type == "demo":
            from trove.services.datasource.demo_setup import setup_demo_datasource
            await setup_demo_datasource(registry, set_default=cfg.default)
        else:
            await registry.register(cfg, set_default=cfg.default)
    except DatasourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "datasource.reconnect", admin, 200, {"name": name})
    return {"datasource": _sanitized(cfg)}


# ── KB init / reload ─────────────────────────────────────


# Spec §4 防重入:同一数据源 init 进行中拒绝重复请求(单事件循环,set 即互斥)
_kb_init_inflight: set[str] = set()


@router.post("/admin/datasources/{name}/kb/init")
async def kb_init_datasource(name: str, request: Request, body: dict | None = None,
                             admin: dict = Depends(require_admin)) -> dict:
    if name in _kb_init_inflight:
        raise HTTPException(status_code=409, detail=f"KB init already running for {name}")
    if not _registry(request).is_registered(name):
        raise HTTPException(status_code=404, detail=f"datasource not found: {name}")
    from trove.services.kb.init_pipeline import init_kb
    _kb_init_inflight.add(name)
    try:
        summary = await init_kb(
            _kb(request), _registry(request),
            llm=request.app.state.llm_gateway,
            config=request.app.state.config,
            datasource=name,
            overwrite=bool((body or {}).get("overwrite")),
        )
    except DatasourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        _kb_init_inflight.discard(name)
    await _audit(request, "kb.init", admin, 200, {"name": name})
    return {"summary": summary}


@router.post("/admin/datasources/{name}/kb/reload")
async def kb_reload_datasource(name: str, request: Request,
                               admin: dict = Depends(require_admin)) -> dict:
    kb = _kb(request)
    if not _registry(request).is_registered(name):
        raise HTTPException(status_code=404, detail=f"datasource not found: {name}")
    await kb.force_sync(name)
    await _audit(request, "kb.reload", admin, 200, {"name": name})
    return {"status": await kb.kb_status(name)}
