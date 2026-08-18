"""Knowledge base / semantic model endpoints.

Reads go through the SQLite mirror (ensure_synced refreshes it
incrementally from YAML — the single source of truth); appends write
straight into the YAML files.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query, Request

from trove.api.schemas import (
    ExampleCreate,
    LessonConfirmResponse,
    LessonCreate,
    TermCreate,
)

router = APIRouter()


def _kb(request: Request):
    return request.app.state.kb


def _datasource(request: Request, datasource: str | None) -> str:
    """Resolve the target datasource: explicit param or registry default."""
    ds = datasource or request.app.state.connector_registry.default_name
    if not ds:
        raise HTTPException(status_code=400, detail="no active datasource")
    return ds


@router.get("/kb/status")
async def kb_status(request: Request) -> dict:
    kb = _kb(request)
    return {"enabled": kb.enabled, "items": await kb.list_items()}


@router.get("/kb/rules")
async def list_rules(request: Request, datasource: str | None = None) -> dict:
    kb = _kb(request)
    ds = _datasource(request, datasource)
    await kb.ensure_synced(ds)
    return {"rules": await kb.list_rules(ds)}


# ── Terms (semantics.yml) ────────────────────────────────


@router.get("/kb/terms")
async def list_terms(
    request: Request,
    q: str | None = None,
    datasource: str | None = None,
) -> dict:
    kb = _kb(request)
    ds = _datasource(request, datasource)
    await kb.ensure_synced(ds)
    if q:
        hits = await kb.search_terms(q, ds)
        return {"terms": [asdict(h) for h in hits]}
    return {"terms": [{"term": name} for name in await kb.list_term_names(ds)]}


@router.post("/kb/terms", status_code=201)
async def create_term(body: TermCreate, request: Request) -> dict:
    ds = _datasource(request, None)
    await _kb(request).append_term(body.model_dump(), ds)
    return {"status": "ok", "term": body.term}


# ── Examples (examples.yml) ──────────────────────────────


@router.get("/kb/examples")
async def list_examples(
    request: Request,
    q: str | None = None,
    datasource: str | None = None,
    limit: int = Query(default=3, ge=1, le=20),
) -> dict:
    kb = _kb(request)
    ds = _datasource(request, datasource)
    await kb.ensure_synced(ds)
    if q:
        hits = await kb.search_examples(q, ds, limit=limit)
        return {"examples": [asdict(h) for h in hits]}
    return {"examples": [{"question": question} for question in await kb.list_example_questions(ds)]}


@router.post("/kb/examples", status_code=201)
async def create_example(body: ExampleCreate, request: Request) -> dict:
    ds = _datasource(request, None)
    await _kb(request).append_example(body.model_dump(), ds)
    return {"status": "ok", "question": body.question}


# ── Lessons (Hint Bank, pending until confirmed) ─────────


@router.get("/kb/lessons")
async def list_lessons(
    request: Request,
    datasource: str | None = None,
    pending: bool = False,
) -> dict:
    kb = _kb(request)
    ds = _datasource(request, datasource)
    await kb.ensure_synced(ds)
    return {"lessons": await kb.list_lessons(ds, confirmed_only=not pending)}


@router.post("/kb/lessons", status_code=201)
async def create_lesson(body: LessonCreate, request: Request) -> dict:
    ds = _datasource(request, None)
    entry = body.model_dump()
    entry["confirmed"] = False
    await _kb(request).append_lesson(entry, ds)
    return {"status": "ok", "pattern": body.pattern}


@router.post("/kb/lessons/confirm", response_model=LessonConfirmResponse)
async def confirm_lessons(request: Request, datasource: str | None = None) -> dict:
    ds = _datasource(request, datasource)
    return {"confirmed": await _kb(request).confirm_pending_lessons(ds)}


# ── Table annotations (schema_notes.yml) ─────────────────


@router.get("/kb/tables/{table_name}/notes")
async def table_notes(table_name: str, request: Request, datasource: str | None = None) -> dict:
    kb = _kb(request)
    ds = _datasource(request, datasource)
    await kb.ensure_synced(ds)
    notes = await kb.table_notes([table_name], ds)
    if table_name not in notes:
        raise HTTPException(status_code=404, detail=f"no notes for table: {table_name}")
    return asdict(notes[table_name])
