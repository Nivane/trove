"""User facts (memory) endpoints — self-service per-user memory layer.

Each authenticated user manages their own facts: short statements of
preference or business caliber scoped to a datasource (e.g. "营收 = 净收入",
"看日均用 30 日均值"). Facts are injected into SQL generation as a
personalization context block. Every endpoint here operates strictly on
the caller's own user_id — a user can never read or mutate another
user's facts through this router (admins use /v1/admin/facts).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import get_current_user
from trove.api.schemas import FactCreate, FactPatch

router = APIRouter()


def _facts(request: Request):
    return request.app.state.user_facts


def _default_datasource(request: Request) -> str:
    registry = request.app.state.connector_registry
    return registry.default_name if registry is not None else None


@router.get("/facts")
async def list_facts(
    request: Request,
    datasource: str | None = None,
    q: str | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    """List my facts; ``q`` previews question-relevance retrieval."""
    svc = _facts(request)
    uid = str(user["id"])
    if q:
        ds = datasource or _default_datasource(request)
        if not ds:
            raise HTTPException(status_code=400, detail="datasource required for search")
        return {"facts": await svc.search(uid, ds, q, limit=10)}
    return {"facts": await svc.list(uid, datasource)}


@router.post("/facts", status_code=201)
async def create_fact(
    body: FactCreate, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    try:
        fact = await _facts(request).add(str(user["id"]), body.datasource, body.fact)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"fact": fact}


@router.patch("/facts/{fact_id}")
async def update_fact(
    fact_id: int, body: FactPatch, request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    try:
        fact = await _facts(request).update(
            str(user["id"]), fact_id, fact=body.fact, datasource=body.datasource,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if fact is None:
        raise HTTPException(status_code=404, detail=f"fact not found: {fact_id}")
    return {"fact": fact}


@router.delete("/facts/{fact_id}", status_code=204)
async def delete_fact(
    fact_id: int, request: Request, user: dict = Depends(get_current_user),
) -> None:
    if not await _facts(request).delete(str(user["id"]), fact_id):
        raise HTTPException(status_code=404, detail=f"fact not found: {fact_id}")
