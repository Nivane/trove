"""Database catalog endpoints (read-only, auth + datasource grants)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from trove.api.deps import get_current_user, require_datasource
from trove.core.errors import DatasourceError

router = APIRouter()


def _catalog(request: Request):
    return request.app.state.catalog_service


def _registry(request: Request):
    return request.app.state.connector_registry


@router.get("/catalog/datasources")
async def list_datasources(
    request: Request, user: dict = Depends(get_current_user)
) -> dict:
    """Datasources visible to the caller: admins see all; users see only
    granted ones (empty grants = the registry default)."""
    registry = _registry(request)
    infos = registry.list_info()
    if user["role"] == "admin":
        return {"datasources": infos}
    auth = request.app.state.auth
    grants = await auth.get_datasources(user["id"])
    if not grants:
        default_name = registry.default_name
        allowed = {default_name} if default_name else set()
    else:
        allowed = set(grants)
    return {"datasources": [i for i in infos if i.get("name") in allowed]}


@router.get("/catalog/tables")
async def list_tables(
    request: Request,
    datasource: str | None = None,
    schema_filter: str | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    ds = await require_datasource(request, datasource, user)
    try:
        tables = await _catalog(request).list_tables(ds, schema_filter)
    except DatasourceError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"tables": tables}


@router.get("/catalog/search")
async def search_tables(
    request: Request,
    q: str = Query(min_length=1),
    datasource: str | None = None,
    limit: int = Query(default=10, ge=1, le=100),
    user: dict = Depends(get_current_user),
) -> dict:
    ds = await require_datasource(request, datasource, user)
    try:
        tables = await _catalog(request).search_tables(q, ds, limit)
    except DatasourceError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"tables": tables}


@router.get("/catalog/tables/{table_name}")
async def table_detail(
    table_name: str, request: Request, datasource: str | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    ds = await require_datasource(request, datasource, user)
    try:
        detail = await _catalog(request).table_detail(table_name, ds)
    except DatasourceError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if detail is None:
        raise HTTPException(status_code=404, detail=f"table not found: {table_name}")
    return detail


@router.get("/catalog/tables/{table_name}/ddl")
async def table_ddl(
    table_name: str, request: Request, datasource: str | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    ds = await require_datasource(request, datasource, user)
    catalog = _catalog(request)
    try:
        detail = await catalog.table_detail(table_name, ds)
    except DatasourceError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if detail is None:
        raise HTTPException(status_code=404, detail=f"table not found: {table_name}")
    return {"ddl": await catalog.get_schema_ddl(table_name, ds)}
