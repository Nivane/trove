"""Database catalog endpoints (read-only, auth + datasource grants)."""

from __future__ import annotations

import csv
import re
from io import StringIO
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File

from trove.api.deps import get_current_user, require_datasource
from trove.core.errors import DatasourceError
from trove.core.types import DatasourceConfig

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
    granted AND initialized ones (empty grants = the registry default)."""
    registry = _registry(request)
    infos = registry.list_info()
    kb = request.app.state.kb
    for info in infos:
        info["kb_initialized"] = bool(kb.init_exists(info["name"]))
        # status 恒为 "connected"：catalog 数据只来自 registry，而 registry 只持有已连接
        # 的数据源；断开态（仅 datasources.yml 里的配置）只出现在管理端列表
        info["status"] = "connected"
    if user["role"] == "admin":
        return {"datasources": infos}
    auth = request.app.state.auth
    grants = await auth.get_datasources(user["id"])
    if not grants:
        default_name = registry.default_name
        allowed = {default_name} if default_name else set()
    else:
        allowed = set(grants)
    return {"datasources": [
        i for i in infos
        if i.get("name") in allowed and i["kb_initialized"]
    ]}


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


def _infer_sql_type(values: list[str]) -> str:
    """Infer a SQLite column type from a column's string values.

    int if every non-empty value parses as an integer, real if every
    value parses as a float, text otherwise. Booleans (t/f, true/false,
    0/1) are stored as INTEGER for natural SQL comparisons.
    """
    parsed_bool = True
    non_empty = [v for v in values if v != ""]
    if non_empty and all(v.lower() in ("true", "false", "t", "f") for v in non_empty):
        return "INTEGER"
    for v in non_empty:
        try:
            int(v)
        except ValueError:
            parsed_bool = False
            break
    else:
        if non_empty:
            return "INTEGER"
    if non_empty and all(_is_float(v) for v in non_empty):
        return "REAL"
    return "TEXT"


def _is_float(v: str) -> bool:
    try:
        float(v)
        return True
    except ValueError:
        return False


def _normalize_datasource_name(filename: str) -> str:
    stem = Path(filename).stem.lower()
    stem = re.sub(r"[^a-z0-9_]+", "_", stem).strip("_")
    return stem[:40] or "upload"


@router.post("/catalog/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
) -> dict:
    """Upload a CSV/TSV file → register as a queryable SQLite datasource.

    The uploaded rows land in a per-file SQLite database under the config
    home (``~/.trove/uploads/<name>.db``) holding a single ``data`` table;
    column types are inferred from the first rows. The datasource is then
    registered so /v1/chat and the catalog can query it immediately.
    """
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="only admins may upload files")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    text = raw.decode("utf-8-sig", errors="replace").lstrip("\ufeff")

    try:
        reader = csv.reader(StringIO(text))
        rows = [row for row in reader if any(cell.strip() for cell in row)]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"cannot parse file: {e}")
    if len(rows) < 2:
        raise HTTPException(status_code=400, detail="need a header row plus data rows")
    header = [c.strip() or f"col{i}" for i, c in enumerate(rows[0])]
    rows = rows[1:]
    if len(set(header)) != len(header):
        raise HTTPException(status_code=400, detail="duplicate column names in header")

    cols = len(header)
    col_values: list[list[str]] = [[] for _ in range(cols)]
    for row in rows:
        for i in range(cols):
            col_values[i].append(row[i] if i < len(row) else "")
    col_types = [_infer_sql_type(vals) for vals in col_values]

    name = _normalize_datasource_name(file.filename or "upload")
    registry = _registry(request)
    seq = 1
    base = name
    while registry.is_registered(name):
        name = f"{base}_{seq}"
        seq += 1

    config = getattr(request.app.state, "config", None)
    home = Path(getattr(config, "home", "~/.trove")).expanduser()
    uploads_dir = home / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    db_path = uploads_dir / f"{name}.db"
    if db_path.exists():
        db_path.unlink()

    import aiosqlite
    conn = await aiosqlite.connect(str(db_path))
    try:
        col_defs = ", ".join(f'"{c}" {t}' for c, t in zip(header, col_types))
        await conn.execute(f"CREATE TABLE data ({col_defs})")
        placeholders = ", ".join(["?"] * cols)
        for row in rows:
            await conn.execute(
                f"INSERT INTO data VALUES ({placeholders})",
                [row[i] if i < len(row) else "" for i in range(cols)],
            )
        await conn.commit()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"cannot create table: {e}")
    finally:
        await conn.close()

    config = DatasourceConfig(
        name=name,
        type="sqlite",
        connection_params={"path": str(db_path)},
        default=False,
    )
    await registry.register(config, set_default=False)
    return {"datasource": name, "rows": len(rows), "columns": header}
