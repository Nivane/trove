"""Chat and session endpoints.

POST /v1/chat streams the agent's typed events (session → thought →
sql → result → done/error) as Server-Sent Events; the session event
carries the session_id (auto-created when omitted).

All endpoints require authentication; sessions are owned by the creating
user. A foreign user's session is answered with 404 (no existence
disclosure, consistent with the SessionError → 404 handler); admins may
access every session.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import get_current_user, require_datasource
from trove.api.schemas import ChatRequest, RenameRequest, ResumeRequest, SessionCreateResponse
from trove.api.sse import sse_response
from trove.core.errors import SessionError

router = APIRouter()


def _manager(request: Request):
    return request.app.state.session_manager


def _assert_owned(session, user: dict) -> None:
    """Ownership check: 404 for foreign sessions (admin bypasses)."""
    if user["role"] != "admin" and session.user_id != str(user["id"]):
        raise HTTPException(status_code=404, detail=f"session not found: {session.session_id}")


async def _load_or_404(request: Request, session_id: str, user: dict):
    try:
        session = await _manager(request).load_session(session_id)
    except SessionError:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    _assert_owned(session, user)
    return session


@router.post("/sessions", status_code=201, response_model=SessionCreateResponse)
async def create_session(
    request: Request, user: dict = Depends(get_current_user)
) -> dict:
    session = await _manager(request).start_session(user_id=str(user["id"]))
    return {"session_id": session.session_id}


@router.get("/sessions")
async def list_sessions(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    user: dict = Depends(get_current_user),
) -> dict:
    user_id = None if user["role"] == "admin" else str(user["id"])
    sessions = await _manager(request).list_sessions(
        user_id=user_id, offset=offset, limit=limit
    )
    # has_more is a heuristic: a full page suggests more sessions may exist
    return {"sessions": sessions, "has_more": len(sessions) == limit}


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    session = await _load_or_404(request, session_id, user)
    return {
        "session_id": session.session_id,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "summary": session.summary,
        "user_id": session.user_id,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "timestamp": m.timestamp.isoformat(),
                "metadata": m.metadata,
            }
            for m in session.messages
        ],
    }


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str, request: Request, user: dict = Depends(get_current_user)
) -> None:
    session = await _load_or_404(request, session_id, user)
    if not await _manager(request).delete_session(session.session_id):
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")


@router.post("/sessions/{session_id}/title")
async def rename_session(
    session_id: str,
    body: RenameRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    session = await _load_or_404(request, session_id, user)
    if not await _manager(request).rename_session(session.session_id, body.title.strip()):
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return {"session_id": session.session_id, "title": body.title.strip()}


@router.get("/sessions/{session_id}/tasks")
async def get_session_tasks(
    session_id: str, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    """当前会话的任务清单(跨轮次 todo 状态)。"""
    session = await _load_or_404(request, session_id, user)
    tasks = await _manager(request).get_tasks(session)
    return {"session_id": session_id, "tasks": tasks}


@router.post("/sessions/{session_id}/resume")
async def resume_session(
    session_id: str, body: ResumeRequest, request: Request,
    user: dict = Depends(get_current_user),
):
    """继续一处 HITL 中断:SSE 事件流(与 /v1/chat 同构)。

    decision=approve_all 且为批内任务时,剩余任务以 auto_approve 继续执行,
    全部事件在此流中推送;其余情形等价于原来的 JSON 终态(以 done 事件产出)。
    """
    session = await _load_or_404(request, session_id, user)
    manager = _manager(request)

    async def events():
        async for event in manager.resume_stream(session, body.decision, body.workflow):
            payload = {k: v for k, v in event.items() if k != "type"}
            yield {"type": event["type"], "data": payload}

    return sse_response(events())


@router.post("/sessions/{session_id}/compact")
async def compact_session(
    session_id: str, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    session = await _load_or_404(request, session_id, user)
    compacted = await _manager(request).compact_session(session)
    return {
        "session_id": session_id,
        "summary": compacted.summary,
        "message_count": len(compacted.messages),
    }


@router.post("/sessions/{session_id}/clear")
async def clear_session(
    session_id: str, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    session = await _load_or_404(request, session_id, user)
    cleared = await _manager(request).clear_session(session)
    return {"session_id": session_id, "message_count": len(cleared.messages)}


@router.post("/chat")
async def chat(
    body: ChatRequest, request: Request, user: dict = Depends(get_current_user)
):
    manager = _manager(request)
    if body.session_id:
        try:
            session = await manager.load_session(body.session_id)
        except SessionError:
            raise HTTPException(status_code=404, detail=f"session not found: {body.session_id}")
        _assert_owned(session, user)
    else:
        session = await manager.start_session(user_id=str(user["id"]))
    # datasource resolution/authorization comes after the ownership check so
    # foreign/missing sessions keep their documented 404 (no existence
    # disclosure) instead of being preempted by a 403
    ds = await require_datasource(request, body.datasource, user)

    async def events():
        yield {"type": "session", "data": {"session_id": session.session_id}}
        async for event in manager.ask_stream(
            session, body.question, body.workflow, datasource=ds,
            is_admin=user["role"] == "admin",
        ):
            payload = {k: v for k, v in event.items() if k != "type"}
            yield {"type": event["type"], "data": payload}

    return sse_response(events())
