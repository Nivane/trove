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

from trove.api.deps import check_api_rate, get_current_user, require_datasource, require_scope
from trove.api.schemas import ChatRequest, RenameRequest, ResumeRequest, SessionCreateResponse
from trove.api.sse import sse_response
from trove.core.errors import SessionError

# 查询/会话面:受限 token 需要声明 ``query`` scope 才能访问(admin scope
# 管理端点由 require_admin 单独裁决)。未声明 scopes 的 token 不限。
router = APIRouter(dependencies=[Depends(require_scope("query"))])


def _manager(request: Request):
    return request.app.state.session_manager


def _assert_owned(session, user: dict) -> None:
    """Ownership check: 404 for foreign sessions (admin bypasses)."""
    if user["role"] != "admin" and session.user_id != str(user["id"]):
        raise HTTPException(status_code=404, detail=f"session not found: {session.session_id}")


async def _replay_subject(
    request: Request, raw: str | None, user: dict, session=None
) -> str | None:
    """校验并归一化 ``on_behalf_of`` → 目标用户 id;``None`` = 不重放。

    四道校验都必须在**SSE 开始之前**跑完:流一旦开出去,状态码已经发出去了,
    再发现「你不是 admin」只能变成一条事件,客户端读不到 403。

    * 非 admin → 403。这是 API 面的门;判定点(会话层)会用 auth 存储里的 role
      再核一次 —— 上层挡不住就等于没挡(P3 的论点是全仓的),两层都留着。
    * 不是自己的会话 → 403。会话层只能按**会话主人**复核发起人,与其让内层
      拒绝、外层放行(错误变成一条难以归因的事件),不如在门口说清楚。
    * 形状不认识(``group:3`` / 空) → 400。**不猜**。
    * 目标不存在 → 404。与「会话不存在」同口径:重放一个不存在的用户没有意义,
      而静默退回按自己的身份跑会让调用方以为看到的是目标视图。
    """
    if raw is None:
        return None
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="on_behalf_of requires an admin token")
    if session is not None and session.user_id != str(user.get("id")):
        raise HTTPException(
            status_code=403, detail="on_behalf_of is only allowed in your own session"
        )
    target = raw.strip()
    if target.startswith("user:"):
        target = target[len("user:"):].strip()
    if not target.isdigit():
        raise HTTPException(status_code=400, detail=f"unsupported subject: {raw!r}")
    auth = getattr(request.app.state, "auth", None)
    row = None
    if auth is not None:
        row = await auth.store.get_user_by_id(int(target))
    if row is None:
        raise HTTPException(status_code=404, detail=f"user not found: {target}")
    return str(row["id"])


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
    replay = await _replay_subject(request, body.on_behalf_of, user, session)

    async def events():
        async for event in manager.resume_stream(
            session, body.decision, body.workflow, scopes=user.get("scopes"),
            on_behalf_of=replay,
        ):
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
    body: ChatRequest, request: Request, user: dict = Depends(get_current_user),
    _rate: None = Depends(check_api_rate),
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
    replay = await _replay_subject(request, body.on_behalf_of, user, session)

    async def events():
        yield {"type": "session", "data": {"session_id": session.session_id}}
        async for event in manager.ask_stream(
            session, body.question, body.workflow, datasource=ds,
            is_admin=user["role"] == "admin",
            scopes=user.get("scopes"),
            on_behalf_of=replay,
        ):
            payload = {k: v for k, v in event.items() if k != "type"}
            yield {"type": event["type"], "data": payload}

    return sse_response(events())
