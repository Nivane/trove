"""Database catalog endpoints (read-only)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from trove.core.errors import DatasourceError

router = APIRouter()


def _catalog(request: Request):
    return request.app.state.catalog_service


def _registry(request: Request):
    return request.app.state.connector_registry


@router.get("/catalog/datasources")
async def list_datasources(request: Request) -> dict:
    registry = _registry(request)
    default = registry.default_name
    return {
        "datasources": [
            {"name": name, "default": name == default}
            for name in registry.list_names()
        ]
    }


@router.get("/catalog/tables")
async def list_tables(
    request: Request,
    datasource: str | None = None,
    schema_filter: str | None = None,
) -> dict:
    try:
        tables = await _catalog(request).list_tables(datasource, schema_filter)
    except DatasourceError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"tables": tables}


@router.get("/catalog/search")
async def search_tables(
    request: Request,
    q: str = Query(min_length=1),
    datasource: str | None = None,
    limit: int = Query(default=10, ge=1, le=100),
) -> dict:
    try:
        tables = await _catalog(request).search_tables(q, datasource, limit)
    except DatasourceError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"tables": tables}


@router.get("/catalog/tables/{table_name}")
async def table_detail(table_name: str, request: Request, datasource: str | None = None) -> dict:
    try:
        detail = await _catalog(request).table_detail(table_name, datasource)
    except DatasourceError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if detail is None:
        raise HTTPException(status_code=404, detail=f"table not found: {table_name}")
    return detail


@router.get("/catalog/tables/{table_name}/ddl")
async def table_ddl(table_name: str, request: Request, datasource: str | None = None) -> dict:
    catalog = _catalog(request)
    try:
        detail = await catalog.table_detail(table_name, datasource)
    except DatasourceError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if detail is None:
        raise HTTPException(status_code=404, detail=f"table not found: {table_name}")
    return {"ddl": await catalog.get_schema_ddl(table_name, datasource)}
