"""Semantic query API — standalone declarative metric/dimension surface.

``POST /v1/semantic/query``: body carries metrics/dimensions/time_grain/
filters/order_by/limit, the query compiles through the authoritative
``SemanticCompiler`` (same logical universe as the NL pipeline) and executes
read-only via the connector registry. Responses include the compiled SQL and
output columns for transparency.

Masking is part of the delivery path, not an extra: rows go through the same
``build_masker`` the graph's masking node uses (field-level ``mask``
declarations in the semantic model). Unlike the graph node — which degrades by
clearing ``state.rows`` and writing ``[ERR:MASKING]`` into the answer — this
endpoint has no answer channel, so a refusal is an HTTP error with no rows in
the body.

Non-admin users are authorized per-datasource via the standard grant surface
(``require_datasource``).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import (
    check_api_rate,
    get_current_user,
    get_principal,
    require_datasource,
)
from trove.api.schemas import SemanticQueryRequest
from trove.core.metrics import record_masking_applied
from trove.services.authz.masking import MaskingError, build_masker
from trove.services.authz.policy import principal_to_wire
from trove.services.semantic_layer.query import (
    SemanticQuery,
    SemanticQueryError,
    build_and_compile,
)

router = APIRouter()


def _kb(request: Request):
    return request.app.state.kb


def _registry(request: Request):
    return request.app.state.connector_registry


def _provider_for(kb, datasource: str, dialect: str):
    from pathlib import Path

    from trove.services.semantic_layer.provider import SemanticLayerProvider

    return SemanticLayerProvider(
        directory=Path.cwd() / ".trove" / "semantic" / datasource,
        datasource=datasource,
        dialect=dialect,
        kb_semantics_path=kb.semantics_path(datasource),
    )


def _model_for(provider, datasource: str):
    model = provider.model()
    if model is None:
        raise HTTPException(
            status_code=404,
            detail=f"no semantic model for datasource: {datasource}",
        )
    return model


@router.post("/semantic/query")
async def semantic_query(
    body: SemanticQueryRequest,
    request: Request,
    user: dict = Depends(get_current_user),
    _rate: None = Depends(check_api_rate),
) -> dict[str, Any]:
    ds = await require_datasource(request, body.datasource, user)
    kb = _kb(request)
    if kb is not None:
        try:
            await kb.ensure_synced(ds)
        except Exception:
            pass
    try:
        adapter = await _registry(request).get(ds)
        dialect = adapter.dialect() or "sqlite"
    except Exception:
        dialect = "sqlite"
    provider = _provider_for(kb, ds, dialect)
    model = _model_for(provider, ds)

    query = SemanticQuery(
        metrics=body.metrics or [],
        dimensions=body.dimensions or [],
        time_grain=dict(body.time_grain) if body.time_grain else None,
        filters=[f.model_dump() for f in (body.filters or [])],
        order_by=list(body.order_by or []),
        limit=body.limit,
    )
    try:
        compiled = build_and_compile(model, query, dialect=dialect)
    except SemanticQueryError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        result = await _registry(request).execute(compiled["sql"], ds)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"execution failed: {e}")

    rows: list[list[Any]] = result.rows
    masking_applied: dict[str, Any] | None = None
    # 构造走 build_masker(与节点/select 预览同一份规则);``None`` = 部署级
    # 关闭,整段跳过 —— 开关语义只在 build_masker 里读。
    apply_masking = build_masker(
        semantic_layer=provider,
        config=getattr(request.app.state, "config", None),
    )
    if apply_masking is not None:
        try:
            principal = await get_principal(request, user)
            rows, masking_applied = apply_masking(
                rows, compiled["columns"],
                principal=principal_to_wire(principal),
                sql=compiled["sql"],
            )
        except MaskingError as e:
            # 没有下层的 answer 通道可承载拒绝(节点会清 rows 并在回答里写
            # [ERR:MASKING]),所以拒绝 = HTTP 错误 + 响应体里没有行。失败
            # 方向取严:降级放行即泄漏。
            raise HTTPException(status_code=500, detail=f"masking refused: {e}")
        for field, mode in (masking_applied.get("fields") or {}).items():
            record_masking_applied(field, str(mode))

    return {
        "sql": compiled["sql"],
        "columns": compiled["columns"],
        "datasets": compiled["datasets"],
        "version": compiled["version"],
        "rows": rows,
        "row_count": len(rows),
        "masking_applied": masking_applied,
    }
