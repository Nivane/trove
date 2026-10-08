"""Semantic layer management endpoints (admin-only, draft approval flow).

Single source of truth stays the datasource's KB ``semantics.yml``; every
mutation goes through a pending draft that an admin confirms (applies to
YAML + re-syncs the mirror) or rejects. Every mutation writes an audit
entry (action, actor, status) like the rest of the admin surface.

语义工作台 P2 地基在这一层加四个面(方案「后端端点」①-④):
``validate``(草稿干跑,纯函数)→ detail 扩展(issue_items / draft.diff /
drift 条目形态)→ ``preview``(内存副本试跑,零副作用)→ ``drafts/batch``
(批量审批,逐条独立 + 逐条审计)。契约形状钉在 ``trove/api/schemas.py``
(pydantic response_model),不在路由里手拼 dict。
"""

from __future__ import annotations

import asyncio
import copy
import yaml
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import get_principal, require_admin, require_admin_or_analyst
from trove.api.schemas import (
    SemanticBatchRequest,
    SemanticBatchResponse,
    SemanticChangeCreate,
    SemanticChangeReject,
    SemanticDraftCreate,
    SemanticPreviewRequest,
    SemanticPreviewResponse,
    SemanticRollbackRequest,
    SemanticValidateRequest,
    SemanticValidateResponse,
)
from trove.api.routers.semantic_query import _execute_timeout_s

router = APIRouter()


def _kb(request: Request):
    return request.app.state.kb


def _registry(request: Request):
    return request.app.state.connector_registry


def _manager(request: Request):
    from trove.services.semantic_layer.manage import SemanticManager
    return SemanticManager(_kb(request))


def _project_root(request: Request):
    """project root —— 定位 ``<root>/.trove/drift/``(与 drift 路由同口径)。"""
    from pathlib import Path

    kb_dir = getattr(_kb(request), "kb_dir", None)
    if kb_dir is not None:
        try:
            return Path(kb_dir).parent.parent
        except (TypeError, ValueError):
            pass
    return Path.cwd()


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
    **显式 skipped**(「没查成」与「没问题」是两回事,空报告不能冒充无漂移)。
    """
    from pathlib import Path

    from trove.services.semantic_layer.provider import SemanticLayerProvider

    try:
        adapter = await _registry(request).get(ds)
        schema = await adapter.get_schema()
    except Exception:
        return {"status": "skipped", "skip_reason": "catalog_unreachable"}
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


async def _drift_view(request: Request, ds: str, dialect: str) -> dict[str, Any]:
    """条目形态的漂移(方案 ③):``{status, skip_reason, checked_at, items[]}``。

    ``items`` = **本次实时检测**的语义层(L2)条目,与 drift store 里的既有
    条目按 ``(level, subject)`` 对齐后补上生命周期(drift_id / first_seen_at /
    seen_count)与影响面快照 —— 页面因此能显示「首次发现于 X,已复现 N 次」,
    而不是每次都像新问题。store 缺失/未跑过检测 → 这些字段为 null,不报错。
    """
    from trove.services.drift import (
        L1,
        DriftStore,
        ImpactSet,
        from_semantic_drift,
        normalize_subject,
    )

    adapted = from_semantic_drift(
        await _semantic_drift(request, ds, dialect), ds)
    stored: dict[tuple[str, str], Any] = {}
    if adapted.items:
        try:
            store = DriftStore(_project_root(request))
            try:
                rows = await store.list_items(ds, limit=500, include_waived=False)
            finally:
                await store.dispose()
            for row in rows:
                if row.level == L1:
                    continue  # L1 由结构层负责;这里是语义层页面的报告
                stored.setdefault(
                    (row.level, normalize_subject(row.subject)), row)
        except Exception:
            stored = {}
    empty_impact = ImpactSet().to_dict()
    items: list[dict[str, Any]] = []
    for item in adapted.items:
        row = stored.get((item.level, normalize_subject(item.subject)))
        items.append({
            "level": item.level,
            "severity": item.severity,
            "subject": item.subject,
            "detail": item.detail,
            "first_seen_at": row.first_seen_at if row else None,
            "seen_count": row.seen_count if row else None,
            "drift_id": row.id if row else None,
            "impact": (
                ImpactSet.from_dict(row.affected).to_dict() if row else empty_impact
            ),
        })
    return {
        "status": adapted.status,
        "skip_reason": adapted.skip_reason,
        "checked_at": adapted.generated_at,
        "items": items,
    }


@router.get("/admin/semantic/{name}")
async def semantic_detail(
    name: str, request: Request, admin: dict = Depends(require_admin_or_analyst),
) -> dict:
    """One datasource's semantic model + lint issues + draft queue + drift.

    ``issues`` 旁附 ``issue_items``(结构化);每条 draft 附服务端算的
    ``diff``;``drift`` 是条目形态(``items[]`` 带生命周期与影响面)。
    """
    ds = _resolve_datasource(request, name)
    await _kb(request).ensure_synced(ds)
    dialect = await _dialect(request, ds)
    result = await _manager(request).detail(ds, dialect=dialect)
    result["drift"] = await _drift_view(request, ds, dialect)
    return {"semantic": result}


@router.get("/admin/semantic/{name}/history")
async def semantic_history(
    name: str, request: Request, admin: dict = Depends(require_admin_or_analyst),
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


# ── 语义工作台 P2 地基:validate / preview / batch ─────────────


@router.post("/admin/semantic/{name}/validate",
             response_model=SemanticValidateResponse)
async def validate_semantic_draft(
    name: str, body: SemanticValidateRequest, request: Request,
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """草稿干跑校验 —— 纯函数,零副作用(不写盘、不进 git、不刷镜像)。

    与 confirm 走同一条应用路径 + 同一份文档 lint,所以 ``ok`` = 「现在提交
    会不会过门禁」;结构化明细(severity/code/target/message/hint)供
    DiffCard 直接渲染。未分类问题落 ``code="lint"``,不丢弃。
    """
    ds = _resolve_datasource(request, name)
    from trove.services.semantic_layer.issues import validate_draft

    return validate_draft(
        _manager(request).document(ds),
        kind=body.kind, action=body.action, name=body.name,
        payload=body.payload, dialect=await _dialect(request, ds),
    )


class _MemorySemanticLayer:
    """预览用的内存语义层 —— ``build_masker`` 只要求 ``.model()``。

    预览时草稿尚未落盘,脱敏判定必须用**应用后的内存模型**:读盘上的旧
    模型会让试跑的脱敏口径停留在改动之前。
    """

    def __init__(self, model: Any) -> None:
        self._model = model

    def model(self) -> Any:
        return self._model


@router.post("/admin/semantic/{name}/preview",
             response_model=SemanticPreviewResponse)
async def preview_semantic_draft(
    name: str, body: SemanticPreviewRequest, request: Request,
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """草稿试跑:应用到**内存副本** → 编译 → 有界执行(含脱敏),零副作用。

    响应与 ``POST /v1/semantic/query`` 同形 + ``warnings``(草稿的 lint
    警告)。校验失败/编译失败 → 422(结构化明细走 /validate);超时 504;
    执行错 500;脱敏拒绝 500 且响应体里没有行 —— 与查询入口同一套失败方向。
    """
    from trove.core.metrics import record_masking_applied
    from trove.services.authz.masking import MaskingError, build_masker
    from trove.services.authz.policy import principal_to_wire
    from trove.services.semantic_layer.issues import validate_draft
    from trove.services.semantic_layer.manage import _apply_draft
    from trove.services.semantic_layer.ossie import parse_ossie
    from trove.services.semantic_layer.query import (
        SemanticQuery,
        SemanticQueryError,
        build_and_compile,
    )

    ds = _resolve_datasource(request, name)
    dialect = await _dialect(request, ds)
    manager = _manager(request)
    document = manager.document(ds)
    draft = {"kind": body.kind, "action": body.action, "name": body.name,
             "payload": body.payload or None}

    check = validate_draft(
        document, kind=body.kind, action=body.action, name=body.name,
        payload=body.payload, dialect=dialect)
    # 预览**只拦硬错误**:warning 级 lint 会拦 confirm(门禁不分级),但试跑
    # 本身零副作用,拦它没有安全收益 —— 反而让 warnings 永远看不到。
    if check["errors"]:
        raise HTTPException(
            status_code=422,
            detail=f"草稿校验未通过: {check['errors'][0]['message']}")

    applied = copy.deepcopy(document) if document else {}
    try:
        _apply_draft(applied, draft, dialect)
    except (ValueError, TypeError) as e:  # validate 已拦;这里是双保险
        raise HTTPException(status_code=422, detail=f"草稿应用失败: {e}")
    try:
        model = parse_ossie(
            yaml.safe_dump(applied, default_flow_style=False,
                           allow_unicode=True, sort_keys=False),
            preferred_dialect=dialect)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"语义模型解析失败: {e}")

    q = body.query
    metrics = [m for m in q.metrics if m]
    if not metrics and body.kind == "metric" and body.action == "upsert":
        metrics = [body.name]  # 试跑新指标本身:不用手填 query.metrics
    if not metrics:
        raise HTTPException(
            status_code=422,
            detail="preview 需要至少一个指标(metric 草稿可省略 query.metrics)")
    query = SemanticQuery(
        metrics=metrics,
        dimensions=[d for d in q.dimensions if d],
        time_grain=dict(q.time_grain) if q.time_grain else None,
        filters=[f.model_dump() for f in q.filters],
        order_by=list(q.order_by),
        limit=q.limit,
    )
    try:
        compiled = build_and_compile(model, query, dialect=dialect)
    except SemanticQueryError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        result = await asyncio.wait_for(
            _registry(request).execute(compiled["sql"], ds),
            timeout=_execute_timeout_s(request),
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"execution timed out after {_execute_timeout_s(request):g}s",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"execution failed: {e}")

    rows: list[list[Any]] = result.rows
    masking_applied: dict[str, Any] | None = None
    apply_masking = build_masker(
        semantic_layer=_MemorySemanticLayer(model),
        config=getattr(request.app.state, "config", None),
    )
    if apply_masking is not None:
        try:
            principal = await get_principal(request, admin)
            rows, masking_applied = apply_masking(
                rows, compiled["columns"],
                principal=principal_to_wire(principal),
                sql=compiled["sql"],
            )
        except MaskingError as e:
            # 与查询入口同一条失败方向:拒绝 = HTTP 错误 + 响应体里没有行。
            raise HTTPException(status_code=500, detail=f"masking refused: {e}")
        for field, mode in (masking_applied.get("fields") or {}).items():
            record_masking_applied(field, str(mode))

    return {
        "sql": compiled["sql"],
        "columns": compiled["columns"],
        "rows": rows,
        "row_count": len(rows),
        "masking_applied": masking_applied,
        "warnings": check["warnings"],
    }


@router.post("/admin/semantic/{name}/drafts/batch",
             response_model=SemanticBatchResponse)
async def batch_semantic_drafts(
    name: str, body: SemanticBatchRequest, request: Request,
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """批量审批:逐条独立(一条失败不影响其余),逐条审计。

    替代前端串行 for 循环 —— 串行时前一条失败会让后面的草稿状态停在
    「不知道跑没跑」;这里每条的成败都在 ``results`` 里显式给出,且成功
    条目写入的 git trailer 标记 ``Generator: semantic.batch``,审计能分清
    逐条点的与批量点的。
    """
    ds = _resolve_datasource(request, name)
    actor = str(admin.get("username", ""))
    manager = _manager(request)
    dialect = await _dialect(request, ds)
    action = body.action
    results: list[dict[str, Any]] = []
    applied = failed = 0
    for draft_id in body.ids:
        error = ""
        draft: dict[str, Any] | None = None
        try:
            if action == "confirm":
                draft = await manager.confirm_draft(
                    ds, draft_id, dialect=dialect, actor=actor,
                    generator="semantic.batch")
            else:
                draft = await manager.reject_draft(
                    ds, draft_id, actor=actor, generator="semantic.batch")
        except KeyError as e:
            error = str(e)
        except ValueError as e:
            error = str(e)
        except Exception as e:  # 单条异常不掀整批
            error = f"{type(e).__name__}: {e}"
        details: dict[str, Any] = {
            "datasource": ds, "id": draft_id, "batch": True,
        }
        if body.note:
            details["note"] = body.note
        if error:
            failed += 1
            results.append({"id": draft_id, "ok": False, "error": error})
            details["error"] = error
            # 失败也审计(单条端点会以 400 落审计;批量条目沿用同一读写形状)
            await _audit(request, f"semantic.draft.{action}", admin, 400, details)
            continue
        applied += 1
        results.append({"id": draft_id, "ok": True, "error": None})
        details["kind"] = draft.get("kind") if draft else None
        details["name"] = draft.get("name") if draft else None
        await _audit(request, f"semantic.draft.{action}", admin, 200, details)
    return {"results": results, "applied": applied, "failed": failed}


def _changes(request: Request):
    from trove.services.semantic_layer.changes import ChangeService

    config = getattr(getattr(request.app.state, "config", None),
                     "semantic_changes", None)
    return ChangeService(_kb(request), config=config)


# ── 语义变更评审（设计 §7.2；声明顺序：静态段在前，{change_id} 在后）──


@router.post("/admin/semantic/{name}/changes", status_code=201)
async def open_semantic_change(
    name: str, body: SemanticChangeCreate, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """显式开单：快照 .staging/ + 记录 status=open（主线不动）。"""
    ds = _resolve_datasource(request, name)
    actor = str(admin.get("username", ""))
    payloads = [p.model_dump() for p in body.payloads]
    try:
        change = await _changes(request).open(
            ds, origin="manual", payloads=payloads, question=body.question,
            note=body.note, author=actor, dialect=await _dialect(request, ds))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "semantic.change.open", admin, 201, {
        "datasource": ds, "id": change["id"],
        "subjects": change["subjects"]})
    return {"change": change}


@router.get("/admin/semantic/{name}/changes")
async def list_semantic_changes(
    name: str, request: Request, status: str = "",
    admin: dict = Depends(require_admin_or_analyst),
) -> dict:
    ds = _resolve_datasource(request, name)
    records = _changes(request).list(ds, status=status or None)
    return {"changes": [r.to_dict() for r in records]}


@router.get("/admin/semantic/{name}/changes/{change_id}")
async def semantic_change_detail(
    name: str, change_id: str, request: Request,
    admin: dict = Depends(require_admin_or_analyst),
) -> dict:
    ds = _resolve_datasource(request, name)
    try:
        detail = _changes(request).detail(ds, change_id,
                                          dialect=await _dialect(request, ds))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"change not found: {change_id}")
    return {"change": detail}


@router.post("/admin/semantic/{name}/changes/{change_id}/verify")
async def verify_semantic_change(
    name: str, change_id: str, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    ds = _resolve_datasource(request, name)
    try:
        verification = _changes(request).verify(ds, change_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"change not found: {change_id}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "semantic.change.verify", admin, 200, {
        "datasource": ds, "id": change_id,
        "verdict": verification.get("verdict")})
    return {"verification": verification}


@router.post("/admin/semantic/{name}/changes/{change_id}/merge")
async def merge_semantic_change(
    name: str, change_id: str, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """合并（★ I1 的唯一写入口的 HTTP 面）。冲突 409 / 门禁不过 422。

    无 ``auto`` 入参（设计 §8.2）：``Auto-approved-by: deterministic-gate``
    只能由确定性门自己触发 —— 客户端可控的查询参数会让审计标记可伪造。
    """
    from trove.services.semantic_layer.changes import ChangeInvalid, ChangeStale

    ds = _resolve_datasource(request, name)
    actor = str(admin.get("username", ""))
    try:
        change = await _changes(request).merge(ds, change_id, by=actor)
    except ChangeStale as e:
        raise HTTPException(status_code=409, detail={"code": "stale_change",
                                                     "message": str(e)})
    except ChangeInvalid as e:
        raise HTTPException(status_code=422, detail={"code": "change_invalid",
                                                     "message": str(e)})
    except KeyError:
        raise HTTPException(status_code=404, detail=f"change not found: {change_id}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "semantic.change.merge", admin, 200, {
        "datasource": ds, "id": change_id,
        "auto": bool(change.get("auto")),
        "warnings": change.get("warnings"), "degraded": change.get("degraded")})
    return {"change": change}


@router.post("/admin/semantic/{name}/changes/{change_id}/reject")
async def reject_semantic_change(
    name: str, change_id: str, body: SemanticChangeReject, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    ds = _resolve_datasource(request, name)
    actor = str(admin.get("username", ""))
    try:
        change = await _changes(request).reject(
            ds, change_id, by=actor, reason=body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"change not found: {change_id}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "semantic.change.reject", admin, 200, {
        "datasource": ds, "id": change_id})
    return {"change": change}
