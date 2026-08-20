"""Chat and session endpoints.

POST /v1/chat streams the agent's typed events (session → thought →
sql → result → done/error) as Server-Sent Events; the session event
carries the session_id (auto-created when omitted).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from trove.api.schemas import ChatRequest, ResumeRequest, SessionCreateResponse
from trove.api.sse import sse_response
from trove.core.errors import SessionError

router = APIRouter()


def _manager(request: Request):
    return request.app.state.session_manager


@router.post("/sessions", status_code=201, response_model=SessionCreateResponse)
async def create_session(request: Request) -> dict:
    session = await _manager(request).start_session()
    return {"session_id": session.session_id}


@router.get("/sessions")
async def list_sessions(request: Request) -> dict:
    return {"sessions": await _manager(request).list_sessions()}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> dict:
    try:
        session = await _manager(request).load_session(session_id)
    except SessionError:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return {
        "session_id": session.session_id,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "summary": session.summary,
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
async def delete_session(session_id: str, request: Request) -> None:
    if not await _manager(request).delete_session(session_id):
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")


def _load_or_404(request: Request, session_id: str):
    try:
        return _manager(request).load_session(session_id)
    except SessionError:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")


@router.post("/sessions/{session_id}/resume")
async def resume_session(session_id: str, body: ResumeRequest, request: Request):
    """继续一处 HITL 中断:用用户决定 resume 被打断的图,返回最终状态。"""
    session = await _load_or_404(request, session_id)
    final = await _manager(request).resume(session, body.decision, body.workflow)
    return {
        "session_id": session_id,
        "hitl_status": final.hitl_status,
        "response": final.final_response,
        "sql": final.sql,
        "row_count": final.row_count,
        "verdict": final.verdict,
        "insights": final.insights,
        "error": final.error,
    }


@router.post("/sessions/{session_id}/compact")
async def compact_session(session_id: str, request: Request) -> dict:
    session = await _load_or_404(request, session_id)
    compacted = await _manager(request).compact_session(session)
    return {
        "session_id": session_id,
        "summary": compacted.summary,
        "message_count": len(compacted.messages),
    }


@router.post("/sessions/{session_id}/clear")
async def clear_session(session_id: str, request: Request) -> dict:
    session = await _load_or_404(request, session_id)
    cleared = await _manager(request).clear_session(session)
    return {"session_id": session_id, "message_count": len(cleared.messages)}


@router.post("/chat")
async def chat(body: ChatRequest, request: Request):
    manager = _manager(request)
    if body.session_id:
        try:
            session = await manager.load_session(body.session_id)
        except SessionError:
            raise HTTPException(status_code=404, detail=f"session not found: {body.session_id}")
    else:
        session = await manager.start_session()

    async def events():
        yield {"type": "session", "data": {"session_id": session.session_id}}
        async for event in manager.ask_stream(session, body.question, body.workflow):
            payload = {k: v for k, v in event.items() if k != "type"}
            yield {"type": event["type"], "data": payload}

    return sse_response(events())
