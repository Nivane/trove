"""Knowledge base / semantic model endpoints.

Reads go through the SQLite mirror (ensure_synced refreshes it
incrementally from YAML — the single source of truth); appends write
straight into the YAML files.

Reads are open to any authenticated user. KB writes (terms/examples) and
the confirm-ALL action are admin-only; POST /v1/kb/lessons and
POST /v1/kb/ratings stay open to any authenticated user — they are the
user feedback channel that produces *pending* lessons for the admin
console to confirm or reject.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from trove.api.deps import get_current_user, require_admin
from trove.api.schemas import (
    ExampleCreate,
    LessonConfirmResponse,
    LessonCreate,
    LessonRatingCreate,
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
async def kb_status(
    request: Request, user: dict = Depends(get_current_user)
) -> dict:
    kb = _kb(request)
    return {"enabled": kb.enabled, "items": await kb.list_items()}


@router.get("/kb/rules")
async def list_rules(
    request: Request, user: dict = Depends(get_current_user),
    datasource: str | None = None,
) -> dict:
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
    user: dict = Depends(get_current_user),
) -> dict:
    kb = _kb(request)
    ds = _datasource(request, datasource)
    await kb.ensure_synced(ds)
    if q:
        hits = await kb.search_terms(q, ds)
        return {"terms": [asdict(h) for h in hits]}
    return {"terms": [{"term": name} for name in await kb.list_term_names(ds)]}


@router.post("/kb/terms", status_code=201)
async def create_term(
    body: TermCreate, request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    ds = _datasource(request, datasource)
    await _kb(request).append_term(body.model_dump(), ds)
    return {"status": "ok", "term": body.term, "datasource": ds}


# ── Examples (examples.yml) ──────────────────────────────


@router.get("/kb/examples")
async def list_examples(
    request: Request,
    q: str | None = None,
    datasource: str | None = None,
    limit: int = Query(default=3, ge=1, le=20),
    user: dict = Depends(get_current_user),
) -> dict:
    kb = _kb(request)
    ds = _datasource(request, datasource)
    await kb.ensure_synced(ds)
    if q:
        hits = await kb.search_examples(q, ds, limit=limit)
        return {"examples": [asdict(h) for h in hits]}
    return {"examples": [{"question": question} for question in await kb.list_example_questions(ds)]}


@router.post("/kb/examples", status_code=201)
async def create_example(
    body: ExampleCreate, request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    ds = _datasource(request, datasource)
    await _kb(request).append_example(body.model_dump(), ds)
    return {"status": "ok", "question": body.question, "datasource": ds}


# ── Pending example drafts (好评闭环:user 好评 → draft → admin 确认) ──


@router.get("/kb/examples/pending")
async def list_pending_examples(
    request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    """待确认参考示例(好评问答自动草拟,pending 不参与检索)。"""
    ds = _datasource(request, datasource)
    return {"examples": await _kb(request).list_pending_examples(ds)}


@router.post("/kb/examples/draft", status_code=201)
async def draft_example(
    body: ExampleCreate, request: Request, datasource: str | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    """User feedback channel: 好评问答 → pending 参考示例,供 admin 确认。"""
    ds = _datasource(request, datasource)
    res = await _kb(request).draft_example(
        body.question, body.sql, ds, tags=body.tags, note="",
    )
    if res.get("status") == "invalid":
        raise HTTPException(status_code=400, detail="question and sql are required")
    return {"status": res["status"], "question": body.question, "datasource": ds}


@router.post("/kb/examples/confirm", response_model=LessonConfirmResponse)
async def confirm_pending_examples(
    request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    """确认全部 pending 示例(清除 pending 标志,进入检索)。"""
    ds = _datasource(request, datasource)
    return {"confirmed": await _kb(request).confirm_pending_examples(ds)}


@router.post("/kb/examples/reject", response_model=LessonConfirmResponse)
async def reject_pending_examples(
    request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    """拒绝(删除)全部 pending 示例。"""
    ds = _datasource(request, datasource)
    return {"confirmed": await _kb(request).reject_pending_examples(ds)}


# ── Lessons (Hint Bank, pending until confirmed) ─────────


@router.get("/kb/lessons")
async def list_lessons(
    request: Request,
    datasource: str | None = None,
    pending: bool = False,
    user: dict = Depends(get_current_user),
) -> dict:
    kb = _kb(request)
    ds = _datasource(request, datasource)
    await kb.ensure_synced(ds)
    return {"lessons": await kb.list_lessons(ds, confirmed_only=not pending)}


@router.post("/kb/lessons", status_code=201)
async def create_lesson(
    body: LessonCreate, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    """User feedback channel: creates a *pending* lesson for admin review."""
    ds = _datasource(request, None)
    entry = body.model_dump()
    entry["confirmed"] = False
    await _kb(request).append_lesson(entry, ds)
    return {"status": "ok", "pattern": body.pattern}


@router.post("/kb/ratings", status_code=201)
async def rate_lesson(
    body: LessonRatingCreate, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    """User up/down vote on a question->answer.

    The rated Q&A is upserted into the lesson Hint Bank keyed by
    `question`, aggregating upvotes/downvotes and landing *pending* for the
    admin console to confirm or reject.
    """
    ds = _datasource(request, None)
    lesson = await _kb(request).rate_lesson(body.model_dump(), ds)
    return {"status": "ok", "question": body.question, "lesson": lesson}


@router.post("/kb/lessons/confirm", response_model=LessonConfirmResponse)
async def confirm_lessons(
    request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    ds = _datasource(request, datasource)
    return {"confirmed": await _kb(request).confirm_pending_lessons(ds)}


# ── Table annotations (schema_notes.yml) ─────────────────


@router.get("/kb/tables/{table_name}/notes")
async def table_notes(
    table_name: str, request: Request, datasource: str | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    kb = _kb(request)
    ds = _datasource(request, datasource)
    await kb.ensure_synced(ds)
    notes = await kb.table_notes([table_name], ds)
    if table_name not in notes:
        raise HTTPException(status_code=404, detail=f"no notes for table: {table_name}")
    return asdict(notes[table_name])
