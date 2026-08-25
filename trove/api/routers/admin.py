"""Admin console endpoints — every route requires admin role.

Users / tokens / datasource grants / audit log / cross-user session view /
per-lesson KB approval. Every mutation writes an audit entry
(action, actor, status) so the management surface is traceable.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

logger = logging.getLogger(__name__)

from trove.api.deps import require_admin
from trove.api.schemas import (
    DatasourcesPut,
    SettingsUpdate,
    TokenCreate,
    UserCreate,
    UserPatch,
)
from trove.core.errors import (
    AuthError,
    DatasourceConflictError,
    DatasourceError,
)
from trove.core.types import DatasourceConfig
from trove.services.datasource.naming import validate_datasource_name
from trove.services.datasource.urls import build_url, parse_datasource_url
from trove.services.kb.locks import KbInitBusy, KbInitLock

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
    return {
        "id": cfg.ds_id, "name": cfg.name, "type": cfg.type,
        "default": bool(cfg.default),
        "retrieval_backend": cfg.retrieval_backend or "builtin",
        "embedding_model": cfg.embedding_model or "",
        "vector_backend": cfg.vector_backend or "sqlite",
        "vector_dsn": cfg.vector_dsn or "",
    }


def _retrieval_backend(body: dict) -> str:
    """解析请求体里的可选检索后端;缺省/空 → 保持 builtin。"""
    rb = (body.get("retrieval_backend") or "").strip()
    if not rb:
        return "builtin"
    from trove.services.kb.backends.registry import backend_names
    allowed = {"builtin", *backend_names()}
    if rb not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported retrieval_backend: {rb} (allowed: {sorted(allowed)})",
        )
    return rb


def _vector_config(body: dict) -> dict:
    """解析并校验检索/向量配置:返回可应用的字段字典。

    - rag 要求 embedding_model(稠密通道依赖 LLM 凭证);
    - vector_backend 仅 sqlite/pgvector;pgvector 要求 vector_dsn。
    """
    rb = _retrieval_backend(body)
    emb = (body.get("embedding_model") or "").strip()
    vb = (body.get("vector_backend") or "sqlite").strip()
    vdsn = (body.get("vector_dsn") or "").strip()
    if rb == "rag" and not emb:
        raise HTTPException(
            status_code=400,
            detail="retrieval_backend 'rag' requires embedding_model "
                   "(dense channel needs an embedding model via the LLM gateway)",
        )
    if vb not in ("sqlite", "pgvector"):
        raise HTTPException(
            status_code=400,
            detail=f"unsupported vector_backend: {vb} (allowed: sqlite, pgvector)",
        )
    if vb == "pgvector" and not vdsn:
        raise HTTPException(
            status_code=400,
            detail="vector_backend 'pgvector' requires vector_dsn (dedicated vector database)",
        )
    return {
        "retrieval_backend": rb,
        "embedding_model": emb,
        "vector_backend": vb,
        "vector_dsn": vdsn,
    }


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
    total = await _auth(request).count_audit(user_id=user_id, action=action)
    return {"audit": entries, "total": total}


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
    configs = _store(request).load_configs()
    cfg_of = {c.name: c for c in configs}
    out = []
    for info in registry.list_info():
        cfg = cfg_of.get(info["name"])
        merged = _sanitized(cfg) if cfg else {"retrieval_backend": "builtin"}
        out.append({
            **info,
            "status": "connected",
            **merged,
            "kb_initialized": kb.kb_initialized(info["name"]),
            "kb_items": (await kb.list_items()).get(info["name"], {}),
        })
    registered = {d["name"] for d in out}
    for cfg in configs:
        if cfg.name not in registered:
            out.append({
                **_sanitized(cfg), "type": cfg.type,
                "status": "disconnected", "kb_initialized": False, "kb_items": {},
            })
    return {"datasources": out}


def _existing_identities(request: Request) -> dict[str, str]:
    """name → ds_id across connected + persisted datasources.

    Conflict source for registration: a name bound to a *different*
    ds_id is a conflict (409); the same ds_id is idempotent (reconnect).
    A persisted id wins over the registry view — the config file is the
    source of truth, so a stale registry copy must surface the conflict
    rather than mask it.
    """
    ids: dict[str, str] = {}
    for info in _registry(request).list_info():
        ids[info["name"]] = info.get("id") or ""
    for cfg in _store(request).load_configs():
        ids[cfg.name] = cfg.ds_id
    return ids


@router.post("/admin/datasources", status_code=201)
async def create_datasource(request: Request, body: dict,
                            admin: dict = Depends(require_admin)) -> dict:
    registry = _registry(request)
    store = _store(request)
    name = (body.get("name") or "").strip()
    url = (body.get("url") or "").strip()
    if not name and not url:
        raise HTTPException(status_code=400, detail="name or url required")

    is_demo = url == "demo" or (name == "demo" and not url)
    if is_demo:
        from trove.services.datasource.demo_setup import setup_demo_datasource
        # 先取当前默认态再注册：无默认时新源成为默认（注册顺序即默认语义），
        # 持久化的 default 必须与注册结果一致，否则重启后 demo 不会被恢复为默认。
        was_default = registry.default_name is None
        await setup_demo_datasource(registry, set_default=was_default)
        cfg = DatasourceConfig(name="demo", type="demo",
                               connection_params={}, credentials={},
                               default=was_default,
                               ds_id=registry.identity_of("demo") or "")
    else:
        try:
            cfg = parse_datasource_url(url)
            if name:
                # 命名规则只约束显式输入的名字（URL 派生的库名常非 slug，
                # 如 mini_dev），registry 另有 path-safety 兜底。
                validate_datasource_name(name)
                cfg = dataclasses.replace(cfg, name=name)  # DatasourceConfig 是 dataclass，无 model_copy
            cfg = registry.ensure_identity(cfg)
        except DatasourceError as e:
            raise HTTPException(status_code=400, detail=str(e))
    cfg = dataclasses.replace(cfg, **_vector_config(body))

    # 冲突先行：同名不同身份 → 409，绝不静默覆盖（连接探测前就拒绝）。
    existing = _existing_identities(request)
    if cfg.name in existing and existing[cfg.name] != cfg.ds_id:
        raise HTTPException(
            status_code=409,
            detail=f"datasource '{cfg.name}' already exists with a different identity",
        )

    if is_demo:
        # demo 由 setup 内部已连接注册；这里只补持久化（幂等替换同身份）。
        store.save_configs(
            [c for c in store.load_configs() if c.ds_id != cfg.ds_id] + [cfg]
        )
    else:
        # 事务性注册：先连成功 → 再落库 → 再入 registry；任一步失败回滚，
        # 不留半状态（连接既建、registry 有、持久层无 或 反之）。
        set_default = cfg.default or registry.default_name is None
        try:
            adapter = await registry.prepare(cfg)
        except DatasourceError as e:
            raise HTTPException(status_code=400, detail=str(e))
        try:
            store.save_configs(
                [c for c in store.load_configs() if c.ds_id != cfg.ds_id] + [cfg]
            )
        except Exception as e:
            await adapter.disconnect()
            raise HTTPException(
                status_code=400,
                detail=f"failed to persist datasource registration (rolled back): {e}",
            ) from e
        try:
            registry.activate(cfg, adapter, set_default)
        except DatasourceConflictError:
            # prepare 与 activate 之间的并发竞态：另一方先抢占了同名身份。
            store.save_configs([c for c in store.load_configs() if c.ds_id != cfg.ds_id])
            await adapter.disconnect()
            raise HTTPException(
                status_code=409,
                detail=f"datasource '{cfg.name}' already exists with a different identity",
            ) from None
        except Exception:
            store.save_configs([c for c in store.load_configs() if c.ds_id != cfg.ds_id])
            await adapter.disconnect()
            raise
    await _audit(request, "datasource.create", admin, 201,
                 {"id": cfg.ds_id, "name": cfg.name, "type": cfg.type})
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


@router.get("/admin/datasources/{name}")
async def get_admin_datasource(name: str, request: Request,
                               admin: dict = Depends(require_admin)) -> dict:
    """One datasource with its connection URL — edit-dialog prefill.

    Admin-only: the URL may embed credentials (the admin manages the
    registration and could read datasources.yml directly anyway).
    """
    store = _store(request)
    registry = _registry(request)
    kb = _kb(request)
    cfg = next((c for c in store.load_configs() if c.name == name), None)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"datasource not found: {name}")
    return {
        "datasource": {
            "id": cfg.ds_id,
            "name": cfg.name,
            "type": cfg.type,
            "url": build_url(cfg),
            "default": bool(cfg.default),
            "status": "connected" if registry.is_registered(name) else "disconnected",
            "kb_initialized": kb.kb_initialized(name),
        }
    }


@router.put("/admin/datasources/{name}")
async def update_datasource(name: str, body: dict, request: Request,
                            admin: dict = Depends(require_admin)) -> dict:
    """Edit a datasource's connection (URL/type). Name and ds_id are immutable.

    Blocked once the datasource has an initialized knowledge base — the KB
    is keyed on the name, so after init the connection is locked. Demo is
    bundled and has no URL to edit.
    """
    registry = _registry(request)
    store = _store(request)
    kb = _kb(request)
    cfg = next((c for c in store.load_configs() if c.name == name), None)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"datasource not found: {name}")
    if cfg.type == "demo":
        raise HTTPException(status_code=400, detail="built-in demo datasource cannot be edited")
    if kb.kb_initialized(name):
        raise HTTPException(
            status_code=409,
            detail="datasource has an initialized knowledge base; edit is disabled",
        )

    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    try:
        new_cfg = parse_datasource_url(url)
        new_cfg = dataclasses.replace(
            new_cfg, name=cfg.name, default=cfg.default, ds_id=cfg.ds_id,
            **_vector_config(body),
        )
        new_cfg = registry.ensure_identity(new_cfg)
    except DatasourceError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 事务性更新:连接探测成功 → 落库 → 换 online。任一步失败回滚,不留半状态。
    try:
        adapter = await registry.prepare(new_cfg)
    except DatasourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        store.save_configs(
            [c for c in store.load_configs() if c.name != name] + [new_cfg],
        )
    except Exception as e:
        await adapter.disconnect()
        raise HTTPException(
            status_code=400,
            detail=f"failed to persist datasource update (rolled back): {e}",
        ) from e
    if registry.is_registered(name):
        await registry.unregister(name)
    try:
        registry.activate(new_cfg, adapter, cfg.default)
    except BaseException:
        await adapter.disconnect()
        raise
    await _audit(request, "datasource.update", admin, 200,
                 {"name": name, "type": new_cfg.type})
    return {"datasource": _sanitized(new_cfg)}


@router.post("/admin/datasources/test-connection")
async def test_datasource_connection(body: dict, request: Request,
                                     admin: dict = Depends(require_admin)) -> dict:
    """Non-destructive connection probe.

    Pass ``url`` to test a candidate connection (edit dialog) or ``name`` to
    test a persisted datasource's stored config (row action). Never mutates
    the registry or the config file. Probe *results* are data → always 200
    with ``{ok, error}``.
    """
    registry = _registry(request)
    store = _store(request)
    url = (body.get("url") or "").strip()
    name = (body.get("name") or "").strip()
    if url:
        if url == "demo":
            return {"ok": True, "error": None}
        try:
            cfg = parse_datasource_url(url)
        except DatasourceError as e:
            return {"ok": False, "error": str(e)}
    elif name:
        cfg = next((c for c in store.load_configs() if c.name == name), None)
        if cfg is None:
            return {"ok": False, "error": f"datasource not found: {name}"}
        if cfg.type == "demo":
            return {"ok": True, "error": None}
    else:
        raise HTTPException(status_code=400, detail="name or url required")
    try:
        adapter = await registry.prepare(cfg)
    except DatasourceError as e:
        return {"ok": False, "error": str(e)}
    await adapter.disconnect()
    return {"ok": True, "error": None}


# ── KB init / reload ─────────────────────────────────────


def _ds_id_or_404(request: Request, name: str) -> tuple[str, str]:
    """Resolve a datasource name to its immutable ds_id.

    Lookup order: connected registry first, then persisted config
    (disconnected sources still carry an identity). Unknown → 404.
    Returns ``(name, ds_id)`` — the name stays the KB storage key, the
    ds_id is what init locking is keyed on.
    """
    reg = _registry(request)
    info = next((i for i in reg.list_info() if i["name"] == name), None)
    if info is not None:
        return name, info.get("id") or ""
    cfg = next((c for c in _store(request).load_configs() if c.name == name), None)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"datasource not found: {name}")
    return name, cfg.ds_id


@router.post("/admin/datasources/{name}/kb/init", status_code=202)
async def kb_init_datasource_async(name: str, request: Request,
                                   body: dict | None = None,
                                   admin: dict = Depends(require_admin)) -> dict:
    """异步启动 KB init:立即返回 task_id,后台执行,GET status 轮询进度。"""
    if not _registry(request).is_registered(name):
        raise HTTPException(status_code=404, detail=f"datasource not found: {name}")
    _, ds_id = _ds_id_or_404(request, name)
    from trove.services.kb.init_pipeline import init_kb
    from trove.services.kb.init_tasks import init_tasks

    task = init_tasks.create(name, ds_id)
    if task is None:
        raise HTTPException(
            status_code=409,
            detail=f"KB init already running for datasource {name}",
        )
    kb = _kb(request)
    registry = _registry(request)
    llm_gateway = request.app.state.llm_gateway
    config = request.app.state.config
    lock = KbInitLock(kb.kb_dir / ".locks")
    overwrite = bool((body or {}).get("overwrite"))
    task_id = task["id"]

    async def _run() -> None:
        try:
            with lock.acquire(ds_id):
                summary = await init_kb(
                    kb, registry,
                    llm=llm_gateway, config=config,
                    datasource=name, overwrite=overwrite,
                    progress=lambda u: init_tasks.update(task_id, **u),
                )
            init_tasks.done(task_id, summary)
            await _audit(request, "kb.init", admin, 200,
                         {"id": ds_id, "name": name})
        except KbInitBusy as e:
            init_tasks.fail(task_id, f"KB init already running: {e}")
        except DatasourceError as e:
            init_tasks.fail(task_id, str(e))
        except Exception as e:
            logger.warning("Async KB init failed (%s): %s", name, e)
            init_tasks.fail(task_id, f"KB init failed: {e}")
        finally:
            init_tasks.release_background(task_id)

    bg = asyncio.create_task(_run())
    init_tasks.bind_background(task_id, bg)
    return {"task_id": task_id, "status": "running", "datasource": name}


def _kb_init_status_sync(kb, datasource: str) -> bool:
    """数据源当前是否已有初始化 KB(状态查询的兜底信号)。"""
    try:
        return kb.kb_initialized(datasource)
    except Exception:
        return False


@router.get("/admin/datasources/{name}/kb/init/status")
async def kb_init_status_datasource(name: str, request: Request,
                                    admin: dict = Depends(require_admin)) -> dict:
    """KB init 任务状态(前端轮询)。无任务 → idle + kb_initialized 兜底。"""
    from trove.services.kb.init_tasks import init_tasks

    task = init_tasks.by_datasource(name)
    initialized = _kb_init_status_sync(_kb(request), name)
    if task is None:
        return {
            "status": "idle",
            "task_id": None,
            "datasource": name,
            "kb_initialized": initialized,
            "progress": None,
        }
    return {
        "task_id": task["id"],
        "datasource": name,
        "status": task["status"],
        "stage": task["stage"],
        "progress": task["progress"],
        "detail": task["detail"],
        "summary": task["summary"],
        "error": task["error"],
        "kb_initialized": initialized,
    }


@router.post("/admin/datasources/{name}/kb/reload", status_code=202)
async def kb_reload_datasource_async(name: str, request: Request,
                                     admin: dict = Depends(require_admin)) -> dict:
    """异步重新同步:立即返回 task_id,后台 force_sync,GET .../reload/status 轮询。

    与 /kb/init 共用 init_tasks 注册表(按 datasource 键控)——同源已有
    running 任务(init 或 reload)时 409,天然互斥同一数据源的文件写。
    """
    if not _registry(request).is_registered(name):
        raise HTTPException(status_code=404, detail=f"datasource not found: {name}")
    from trove.services.kb.init_tasks import init_tasks

    task = init_tasks.create(name)
    if task is None:
        raise HTTPException(
            status_code=409,
            detail=f"KB reload already running for datasource {name}",
        )
    task_id = task["id"]
    kb = _kb(request)

    async def _run() -> None:
        try:
            await kb.force_sync(name)
            await _audit(request, "kb.reload", admin, 200, {"name": name})
            init_tasks.done(task_id, "")
        except Exception as e:
            logger.warning("Async KB reload failed (%s): %s", name, e)
            init_tasks.fail(task_id, f"KB reload failed: {e}")
        finally:
            init_tasks.release_background(task_id)

    bg = asyncio.create_task(_run())
    init_tasks.bind_background(task_id, bg)
    return {"task_id": task_id, "status": "running", "datasource": name}


@router.get("/admin/datasources/{name}/kb/reload/status")
async def kb_reload_status_datasource(name: str, request: Request,
                                      admin: dict = Depends(require_admin)) -> dict:
    """KB reload 任务状态(前端轮询)。无任务 → idle。"""
    from trove.services.kb.init_tasks import init_tasks

    task = init_tasks.by_datasource(name)
    if task is None:
        return {
            "status": "idle",
            "task_id": None,
            "datasource": name,
            "stage": None,
            "progress": None,
            "detail": None,
            "summary": None,
            "error": None,
        }
    return {
        "task_id": task["id"],
        "datasource": name,
        "status": task["status"],
        "stage": task["stage"],
        "progress": task["progress"],
        "detail": task["detail"],
        "summary": task["summary"],
        "error": task["error"],
    }


@router.get("/admin/datasources/{name}/kb")
async def kb_detail_datasource(name: str, request: Request,
                               admin: dict = Depends(require_admin)) -> dict:
    """Full KB detail of one datasource for the management page.

    The mirror is refreshed (ensure_synced) before reading so freshly
    written YAML (manual edits, /kb learn, appends) shows up immediately.
    """
    kb = _kb(request)
    if not _registry(request).is_registered(name):
        raise HTTPException(status_code=404, detail=f"datasource not found: {name}")
    try:
        await kb.ensure_synced(name)
        detail = await kb.kb_detail(name)
    except DatasourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"kb": detail}


@router.delete("/admin/datasources/{name}/kb", status_code=200)
async def kb_delete_datasource(name: str, request: Request,
                               admin: dict = Depends(require_admin)) -> dict:
    """Delete a datasource's knowledge base entirely.

    Completes the delete story (previously only datasource registration
    was removable; KB files lingered on disk). Removes the KB directory,
    purges the mirror rows and drops the per-ds init lock — idempotent:
    deleting an already-empty KB returns the same empty status.
    """
    kb = _kb(request)
    ds_id = ""
    if _registry(request).is_registered(name):
        ds_id = _registry(request).identity_of(name) or ""
    else:
        cfg = next((c for c in _store(request).load_configs() if c.name == name), None)
        if cfg is not None:
            ds_id = cfg.ds_id
    if not kb.init_exists(name):
        raise HTTPException(status_code=404, detail=f"no KB for datasource: {name}")
    try:
        await kb.delete_kb(name)
    except DatasourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 释放按 ds_id 预留的跨进程 init 锁文件(pending init 不会被悬挂)。
    if ds_id:
        lock_path = kb.kb_dir / ".locks" / f"{ds_id}.lock"
        lock_path.unlink(missing_ok=True)
    await _audit(request, "kb.delete", admin, 200, {"id": ds_id, "name": name})
    # 返回删除后的空状态(数据源本身可能未注册,不复用 detail 端点的 404 校验)
    await kb.ensure_synced(name)
    return {"kb": await kb.kb_detail(name)}


# ── Runtime settings (DB-backed; agent.yml is never written) ──


def _settings(request: Request):
    return getattr(request.app.state, "settings", None)


def _runtime_config(request: Request):
    return getattr(request.app.state, "config", None)


@router.get("/admin/settings")
async def get_settings(request: Request, admin: dict = Depends(require_admin)) -> dict:
    """Current runtime settings + which keys have DB overrides.

    API keys are masked: clients only ever see names/endpoints and a
    has_api_key flag — the secret round-trips as a mask sentinel on PUT.
    """
    store = _settings(request)
    config = _runtime_config(request)
    from trove.services.admin_settings import service as settings_service

    values = settings_service.effective_values(config)
    if store is not None:
        from trove.services.admin_settings.service import MASK

        stored = await store.get_all()
        masked = {
            k: settings_service.mask_providers(v)
            if k == "llm.providers" else v
            for k, v in stored.items()
        }
        return {"values": values, "stored": masked, "mask": MASK}
    return {"values": values, "stored": {}, "mask": MASK}


@router.put("/admin/settings")
async def put_settings(
    body: SettingsUpdate, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """Validate + persist + hot-apply a partial settings update.

    Unknown keys or invalid values return 400 listing every error; on
    success the update takes effect immediately (the shared AgentConfig is
    mutated in place) and is persisted for the next boot.
    """
    store = _settings(request)
    config = _runtime_config(request)
    from trove.services.admin_settings import service as settings_service

    if not body.values:
        return {"values": settings_service.effective_values(config)}

    # resolve masked api_keys against the *stored* providers (falling back
    # to what the runtime config currently holds)
    current_providers = None
    if store is not None:
        current_providers = (await store.get_all()).get("llm.providers")
    if current_providers is None:
        current_providers = settings_service.mask_providers(config.providers)

    coerced, errors = settings_service.validate_values(body.values, current_providers)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    if store is not None:
        await store.put_many(coerced)
    settings_service.apply_overrides(config, coerced)
    # 结果限制热更新同步到 pipeline 节点可读的进程级注册表
    from trove.services.limits import set_result_limits
    set_result_limits(config.result_max_rows, config.result_display_rows)
    # hot-swap gateway providers so key/base changes apply without restart
    gateway = getattr(request.app.state, "llm_gateway", None)
    if gateway is not None and "llm.providers" in coerced:
        gateway.set_providers(config.providers)
    await _audit(request, "admin.settings.update", admin, 200,
                 {"keys": sorted(coerced)})
    return {"values": settings_service.effective_values(config)}


# ── User facts (per-user memory) — cross-user view/delete ──


def _user_facts(request: Request):
    return request.app.state.user_facts


@router.get("/admin/facts")
async def list_all_facts(
    request: Request,
    user_id: str | None = None,
    datasource: str | None = None,
    admin: dict = Depends(require_admin),
) -> dict:
    """Every user fact across all users (optionally filtered)."""
    return {"facts": await _user_facts(request).list_all(
        user_id=user_id, datasource=datasource,
    )}


@router.delete("/admin/facts/{fact_id}", status_code=204)
async def delete_any_fact(
    fact_id: int, request: Request, admin: dict = Depends(require_admin),
) -> None:
    """Admin delete of any user's fact (e.g. stale/incorrect memory)."""
    if not await _user_facts(request).delete_any(fact_id):
        raise HTTPException(status_code=404, detail=f"fact not found: {fact_id}")
    await _audit(request, "admin.facts.delete", admin, 204, {"fact_id": fact_id})
