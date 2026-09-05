"""Semantic query API — standalone declarative metric/dimension surface.

``POST /v1/semantic/query``: body carries metrics/dimensions/time_grain/
filters/order_by/limit, the query compiles through the authoritative
``SemanticCompiler`` (same logical universe as the NL pipeline) and executes
read-only via the connector registry. Responses include the compiled SQL and
output columns for transparency.

Non-admin users are authorized per-datasource via the standard grant surface
(``require_datasource``).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import get_current_user, require_datasource
from trove.api.schemas import SemanticQueryRequest
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


def _model_for(kb, datasource: str, dialect: str):
    from pathlib import Path

    from trove.services.semantic_layer.provider import SemanticLayerProvider

    provider = SemanticLayerProvider(
        directory=Path.cwd() / ".trove" / "semantic" / datasource,
        datasource=datasource,
        dialect=dialect,
        kb_semantics_path=kb.semantics_path(datasource),
    )
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
    model = _model_for(kb, ds, dialect)

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

    return {
        "sql": compiled["sql"],
        "columns": compiled["columns"],
        "datasets": compiled["datasets"],
        "version": compiled["version"],
        "rows": result.rows,
        "row_count": len(result.rows),
    }
