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
from trove.api.schemas import SemanticDraftCreate, SemanticRollbackRequest

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


async def _semantic_drift(request: Request, ds: str, dialect: str) -> dict[str, Any]:
    """实时 catalog vs 语义模型声明的漂移报告(admin 展示用,零 LLM)。

    语义层是权威可答边界,声明的表/字段/键/关系端点与物理 schema 不一致
    (stale)时在管理端提示重跑 ``/kb init`` 或修正声明;catalog 获取失败 →
    空报告(不误报)。
    """
    from pathlib import Path

    from trove.services.semantic_layer.provider import SemanticLayerProvider

    try:
        adapter = await _registry(request).get(ds)
        schema = await adapter.get_schema()
    except Exception:
        return {"stale": False}
    catalog = {
        t.name.lower(): {c.name.lower() for c in t.columns}
        for t in schema.tables
    }
    provider = SemanticLayerProvider(
        directory=Path.cwd() / ".trove" / "semantic" / ds,
        datasource=ds,
        dialect=dialect,
        catalog=catalog,
        kb_semantics_path=_kb(request).semantics_path(ds),
    )
    return provider.drift()


@router.get("/admin/semantic/{name}")
async def semantic_detail(
    name: str, request: Request, admin: dict = Depends(require_admin),
) -> dict:
    """One datasource's semantic model + lint issues + draft queue + drift."""
    ds = _resolve_datasource(request, name)
    await _kb(request).ensure_synced(ds)
    dialect = await _dialect(request, ds)
    result = await _manager(request).detail(ds, dialect=dialect)
    result["drift"] = await _semantic_drift(request, ds, dialect)
    return {"semantic": result}


@router.get("/admin/semantic/{name}/history")
async def semantic_history(
    name: str, request: Request, admin: dict = Depends(require_admin),
) -> dict:
    """该数据源 KB 文件的 git 提交历史(管理端变更时间线)。

    语义即代码:每次写操作都产生一条 commit,这里把 git log 直接暴露
    给管理端。非 git 环境(dev 常见)返回空列表,不报错。
    """
    ds = _resolve_datasource(request, name)
    limit = request.query_params.get("limit", "50")
    try:
        limit = max(1, min(200, int(limit)))
    except ValueError:
        limit = 50
    history = await _kb(request).git_history(ds, limit=limit)
    return {"datasource": ds, "history": history}


@router.post("/admin/semantic/{name}/rollback")
async def semantic_rollback(
    name: str, body: SemanticRollbackRequest, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """回滚该数据源 KB 到指定 commit(新建提交,不改写历史)。

    ``sha`` 必须是该数据源 KB 文件的真实历史 commit;git 环境缺失/坏
    sha → 400。回滚同样以一条新 commit 落盘,审计链完整。
    """
    ds = _resolve_datasource(request, name)
    actor = str(admin.get("username", ""))
    result = await _kb(request).git_rollback(
        ds, body.sha, message=body.message or f"kb rollback {ds}",
        trailers={"Generator": "semantic.rollback", "Approved-by": actor} if actor else None,
    )
    if not result.get("rolled_back"):
        raise HTTPException(status_code=400, detail=result)
    await _audit(request, "semantic.rollback", admin, 200, {
        "datasource": ds, "sha": body.sha,
    })
    return {"rolled_back": True, "datasource": ds, "sha": body.sha}


@router.post("/admin/semantic/{name}/drafts", status_code=201)
async def create_semantic_draft(
    name: str, body: SemanticDraftCreate, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """Create a pending draft (semantic_drafts.yml). semantics.yml untouched."""
    ds = _resolve_datasource(request, name)
    actor = str(admin.get("username", ""))
    try:
        draft = await _manager(request).create_draft(
            ds, body.kind, body.action, body.name, body.payload or None, body.note,
            actor=actor)
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
    actor = str(admin.get("username", ""))
    try:
        draft = await _manager(request).confirm_draft(
            ds, draft_id, dialect=await _dialect(request, ds), actor=actor)
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
    actor = str(admin.get("username", ""))
    try:
        draft = await _manager(request).reject_draft(ds, draft_id, actor=actor)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "semantic.draft.reject", admin, 200, {
        "datasource": ds, "id": draft_id, "kind": draft["kind"], "name": draft["name"],
    })
    return {"draft": draft}
