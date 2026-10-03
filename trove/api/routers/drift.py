"""漂移治理端点(admin)。

**状态码各自的含义不能混**:

===  ==========================================================
200/201  成功
400      请求本身不合法(未知级别、缺 reason、严重度非法)
404      条目不存在 / 数据源不存在
409      状态不允许该操作(对已 resolved 的条目再 resolve)
503      **检查未能完成** —— catalog 不可达 / KB 缺失 / 未接语义层
===  ==========================================================

**503 是这里最重要的一条**。在此之前 ``check_drift.py`` 在 catalog 连不上时
返回一份空报告、exit 0,CI 报绿 —— 「数据库连不上的时候,漂移门禁是最绿的」。
HTTP 这边同理:一次没跑成的检查若回 200 + 空列表,调用方(管理台、CI、任何
消费方)看到的与「查过且干净」完全一样。503 是那条分界线,响应体里带
``skip_reason``,让调用方知道该去修什么。

所以本路由**不吞异常也不降级**:拿到 skipped 就回 503,不做「至少返回已有
条目」这种看似友好的处理 —— 那正是把未知洗成通过。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import require_admin, require_admin_or_analyst
from trove.api.schemas import DriftDeclareRequest, DriftResolveRequest
from trove.services.drift import (
    DriftError,
    DriftNotFound,
    DriftService,
    ExternalDrift,
    IllegalTransition,
    UnknownLevel,
)

router = APIRouter()


def _kb(request: Request):
    return request.app.state.kb


def _registry(request: Request):
    return request.app.state.connector_registry


def _project_root(request: Request) -> Path:
    """project root —— 用来定位 ``<root>/.trove/drift/``。

    ``KbService`` 只存 ``kb_dir``(``<root>/.trove/kb``),没有 root 字段,
    所以从它反推;反推不出就退回 cwd。落错目录不影响正确性,只影响文件位置,
    因此这里不值得为它抛错。
    """
    kb_dir = getattr(_kb(request), "kb_dir", None)
    if kb_dir is not None:
        try:
            return Path(kb_dir).parent.parent
        except (TypeError, ValueError):  # kb_dir 不是路径(测试替身)
            pass
    return Path.cwd()


async def _live_schema(request: Request, ds: str) -> dict[str, set[str]]:
    """活库的 ``{表: {列}}``。取不到就**抛**,由 detect 记成 catalog_unreachable。

    异常必须冒出去:吞掉它返回 ``{}`` 会让检测器看到「活库一张表都没有」,
    于是把 KB 里所有表报成 gone —— 一次连不上库的故障被写成几十条漂移,
    比不报还糟。
    """
    adapter = await _registry(request).get(ds)
    schema = await adapter.get_schema()
    return {
        str(t.name).lower(): {str(c.name).lower() for c in t.columns}
        for t in schema.tables
    }


def _service(request: Request, ds: str) -> DriftService:
    from trove.services.drift import DriftStore
    from trove.services.semantic_layer.provider import SemanticLayerProvider

    kb = _kb(request)
    root = _project_root(request)

    async def schema_provider(datasource: str) -> dict[str, set[str]]:
        return await _live_schema(request, datasource)

    async def semantic_factory(datasource: str, catalog: dict[str, set[str]]):
        """没有 ``semantics.yml`` → None(**不适用**,不是查失败)。

        两者必须分开:前者是「这个库没有语义契约可违反」,后者是「有契约但
        读不出来」。把不适用算成失败,会让每个没有语义层的库永久红。
        """
        path = kb.semantics_path(datasource)
        if not path.exists():
            return None
        try:
            adapter = await _registry(request).get(datasource)
            dialect = adapter.dialect() or "sqlite"
        except Exception:  # noqa: BLE001
            dialect = "sqlite"  # 方言只影响表达式解析,取不到不该让整次检查失败
        return SemanticLayerProvider(
            directory=root / ".trove" / "semantic" / datasource,
            datasource=datasource,
            dialect=dialect,
            kb_semantics_path=path,
            catalog=catalog,
        )

    return DriftService(
        root, kb=kb, schema_provider=schema_provider,
        semantic_factory=semantic_factory, store=DriftStore(root),
    )


def _ds_or_404(request: Request, name: str) -> str:
    if not name:
        raise HTTPException(status_code=400, detail="datasource is required")
    if not _registry(request).is_registered(name):
        raise HTTPException(status_code=404, detail="datasource not found")
    return name


def _query_ds(request: Request) -> str:
    return _ds_or_404(request, request.query_params.get("datasource", ""))


async def _audit(request: Request, action: str, user: dict, status: int,
                 details: dict[str, Any] | None = None) -> None:
    await request.app.state.auth.record_audit(
        action, user=user, method=request.method, path=request.url.path,
        status=status, details=details,
    )


def _row_payload(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "datasource": row.datasource,
        "level": row.level,
        "kind": row.kind,
        "subject": row.subject,
        "severity": row.severity,
        "status": row.status,
        "source": row.source,
        "detail": row.detail,
        "affected": row.affected,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "seen_count": row.seen_count,
        "resolved_at": row.resolved_at,
        "resolved_by": row.resolved_by,
        "resolve_reason": row.resolve_reason,
    }


@router.post("/admin/drift/check")
async def drift_check(
    request: Request, admin: dict = Depends(require_admin),
) -> dict:
    """跑一次检测(默认全级别)。未完成 → **503**,不是 200 + 空列表。"""
    ds = _query_ds(request)
    levels = [lv for lv in request.query_params.getlist("level") if lv]

    svc = _service(request, ds)
    try:
        report = await svc.detect(ds, levels=set(levels) if levels else None)
    except UnknownLevel as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await svc.dispose()

    if not report.ok:
        # 不降级、不给「至少返回已有条目」:那会把未知洗成通过。
        raise HTTPException(status_code=503, detail=report.to_dict())

    await _audit(request, "drift.check", admin, 200, {
        "datasource": ds, "detected": len(report.items),
        "new": report.new_count, "levels": sorted(report.levels_verified),
    })
    return report.to_dict()


@router.get("/admin/drift")
async def drift_list(
    request: Request, admin: dict = Depends(require_admin_or_analyst),
) -> dict:
    """列出该数据源的漂移条目(默认不含已豁免的)。"""
    ds = _query_ds(request)
    status = request.query_params.get("status") or None
    level = request.query_params.get("level") or None
    include_waived = request.query_params.get("include_waived") == "true"

    svc = _service(request, ds)
    try:
        rows = await svc.list(ds, status=status, level=level, limit=500)
    finally:
        await svc.dispose()
    if not include_waived and status is None:
        rows = [r for r in rows if r.status != "waived"]
    return {"datasource": ds, "count": len(rows),
            "items": [_row_payload(r) for r in rows]}


@router.get("/admin/drift/runs")
async def drift_runs(
    request: Request, admin: dict = Depends(require_admin_or_analyst),
) -> dict:
    """检测历史 —— **含 skipped 的 run**。I3 要求「未检测」在历史里也看得见。"""
    ds = _query_ds(request)
    svc = _service(request, ds)
    try:
        runs = await svc.runs(ds, limit=50)
    finally:
        await svc.dispose()
    return {"datasource": ds, "runs": runs}


@router.get("/admin/drift/{drift_id}")
async def drift_detail(
    drift_id: int, request: Request, admin: dict = Depends(require_admin_or_analyst),
) -> dict:
    ds = _query_ds(request)
    svc = _service(request, ds)
    try:
        row = await svc.get(drift_id)
        impact = await svc.impact(drift_id)
    except DriftNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        await svc.dispose()
    return {"drift": _row_payload(row), "impact": impact.to_dict()}


async def _transition(request: Request, drift_id: int, admin: dict,
                      status: str, body: DriftResolveRequest) -> dict:
    ds = _query_ds(request)
    # 操作者取登录身份;没有身份(测试替身)才退回请求体里的 by。
    actor = str(admin.get("username", "")) or body.by or "unknown"

    svc = _service(request, ds)
    try:
        if status == "resolved":
            await svc.resolve(drift_id, by=actor, reason=body.reason)
        else:
            await svc.waive(drift_id, by=actor, reason=body.reason)
        row = await svc.get(drift_id)
    except DriftNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except IllegalTransition as e:
        raise HTTPException(status_code=409, detail=str(e))
    finally:
        await svc.dispose()

    await _audit(request, f"drift.{status}", admin, 200, {
        "datasource": ds, "id": drift_id, "reason": body.reason,
    })
    return {"drift": _row_payload(row)}


@router.post("/admin/drift/{drift_id}/resolve")
async def drift_resolve(
    drift_id: int, body: DriftResolveRequest, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """标记已解决。重现已解决项会在下次检测时**重开**,并保留本次理由。"""
    return await _transition(request, drift_id, admin, "resolved", body)


@router.post("/admin/drift/{drift_id}/waive")
async def drift_waive(
    drift_id: int, body: DriftResolveRequest, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """豁免。**与 resolve 的区别**:豁免是对立场的断言,再次检出不会重开。"""
    return await _transition(request, drift_id, admin, "waived", body)


@router.post("/admin/drift/external", status_code=201)
async def drift_declare_external(
    body: DriftDeclareRequest, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """承接 L4 口径漂移声明 —— 上游逻辑变更没有检测器,只有人的告知。"""
    ds = _ds_or_404(request, body.datasource)
    actor = str(admin.get("username", "")) or body.author
    svc = _service(request, ds)
    try:
        row = await svc.declare_external(ExternalDrift(
            datasource=ds, subject=body.subject, kind=body.kind,
            detail=body.detail, severity=body.severity, author=actor,
        ))
    except DriftError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await svc.dispose()

    await _audit(request, "drift.declare_external", admin, 201, {
        "datasource": ds, "subject": body.subject, "kind": body.kind,
    })
    return {"drift": _row_payload(row)}
