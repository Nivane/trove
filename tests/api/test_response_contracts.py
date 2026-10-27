"""Read-endpoint response contracts — exact key sets and wire order.

``response_model`` 不是文档:FastAPI 会**按模型过滤**载荷,未声明的键在
线上静默消失。所以这一组测试不查"包含某键",而是逐字钉住整份键集(顶层
与每个条目)以及键的顺序(模型字段顺序 = 线上 JSON 字节顺序)。

新增一个字段 → 这里红 → 同时改模型与测试;模型漏抄一个键 → 这里也红
(丢失发生在断言之前)。
"""

from __future__ import annotations

from trove.core.types import Message

RUN_ID = "33333333-3333-3333-3333-333333333333"

# 快照,不推导自模型(推导出来的断言永远通过 —— 它要钉的是"今天的线").
SESSION_LIST_KEYS = ["sessions", "has_more"]
SESSION_ITEM_KEYS = [
    "session_id", "project_name", "user_id", "created_at", "updated_at",
    "message_count", "title", "pinned", "size_bytes",
]
SESSION_DETAIL_KEYS = [
    "session_id", "created_at", "updated_at", "summary", "user_id", "messages",
]
SESSION_MESSAGE_KEYS = ["role", "content", "timestamp", "metadata"]
RUN_REPLAY_KEYS = [
    "run_id", "source", "session_id", "question", "started_at", "complete",
    "model", "summary", "timeline", "llm_calls", "tools",
]
TIMELINE_ITEM_KEYS = ["name", "seq", "depth", "elapsed_ms", "tokens", "status"]
LLM_CALL_KEYS = ["node", "model", "elapsed_ms", "tokens"]
TOOL_CALL_KEYS = ["name", "node"]


async def _seed_run(api_app, *, run_id: str) -> str:
    """落一条带 run_id 的 assistant 消息(与会话真实落库形状同源)."""
    manager = api_app.state.session_manager
    session = await manager.start_session(user_id="1")
    session.messages.append(Message(role="user", content="有多少学生?"))
    session.messages.append(Message(
        role="assistant",
        content="共 3 名学生。",
        metadata={"summary": {"run_id": run_id, "question": "有多少学生?"}},
    ))
    await manager.save_session(session)
    return session.session_id


class TestSessionContracts:
    async def test_session_list_item_is_the_full_nine_key_row(self, client):
        assert (await client.post("/v1/sessions")).status_code == 201

        body = (await client.get("/v1/sessions")).json()

        assert list(body) == SESSION_LIST_KEYS
        assert len(body["sessions"]) == 1
        item = body["sessions"][0]
        # size_bytes 在单库多表模型下恒为 0 —— 但它必须出现在线上载荷里,
        # 手写前端类型曾经只投影五个键。
        assert list(item) == SESSION_ITEM_KEYS
        assert item["size_bytes"] == 0
        assert item["message_count"] == 0
        assert item["pinned"] is False
        assert isinstance(body["has_more"], bool)

    async def test_session_detail_and_message_keys(self, client, api_app):
        manager = api_app.state.session_manager
        created = (await client.post("/v1/sessions")).json()["session_id"]
        session = await manager.load_session(created)
        session.messages.append(Message(role="user", content="q"))
        session.messages.append(Message(role="assistant", content="a", metadata={"steps": []}))
        await manager.save_session(session)

        body = (await client.get(f"/v1/sessions/{created}")).json()

        assert list(body) == SESSION_DETAIL_KEYS
        assert body["summary"] is None
        assert len(body["messages"]) == 2
        for msg in body["messages"]:
            assert list(msg) == SESSION_MESSAGE_KEYS
        assert body["messages"][1]["role"] == "assistant"
        assert body["messages"][1]["metadata"] == {"steps": []}

    async def test_missing_session_404_detail_only(self, client):
        resp = await client.get("/v1/sessions/nope")

        assert resp.status_code == 404
        assert list(resp.json()) == ["detail"]


class TestRunReplayContracts:
    async def test_session_fallback_keys(self, client, api_app):
        await _seed_run(api_app, run_id=RUN_ID)

        body = (await client.get(f"/v1/runs/{RUN_ID}")).json()

        assert list(body) == RUN_REPLAY_KEYS
        assert body["source"] == "session"
        assert body["started_at"] is None
        assert body["timeline"] == []
        assert body["llm_calls"] == []
        assert body["tools"] == []

    async def test_trace_item_keys(self, client, api_app, tmp_path):
        from trove.tracing.local import add_event, configure_trace_store

        session_id = await _seed_run(api_app, run_id=RUN_ID)
        configure_trace_store(tmp_path)
        span = f"{RUN_ID}:1"
        add_event(RUN_ID, {
            "kind": "run", "session_id": session_id, "question": "有多少学生?",
            "ts": 1750000000.0,
        })
        add_event(RUN_ID, {
            "kind": "span_start", "span_id": span, "parent_id": None,
            "name": "gen_sql", "seq": 1, "ts": 1750000000.0,
        })
        add_event(RUN_ID, {
            "kind": "llm", "node": "gen_sql", "model": "mock/gen",
            "elapsed_ms": 12.5, "parent_id": span,
            "tokens": {"prompt": 1, "completion": 2, "total": 3},
        })
        add_event(RUN_ID, {
            "kind": "tool", "name": "validate_sql", "parent_id": span,
        })
        add_event(RUN_ID, {
            "kind": "span_end", "span_id": span, "elapsed_ms": 120,
            "tokens": {"prompt": 1, "completion": 2, "total": 3},
        })
        add_event(RUN_ID, {"kind": "finish", "summary": {"model": "mock/gen"}})

        body = (await client.get(f"/v1/runs/{RUN_ID}")).json()

        assert list(body) == RUN_REPLAY_KEYS
        assert body["source"] == "trace"
        assert [list(item) for item in body["timeline"]] == [TIMELINE_ITEM_KEYS]
        assert body["timeline"][0]["elapsed_ms"] == 120
        assert [list(item) for item in body["llm_calls"]] == [LLM_CALL_KEYS]
        # 浮点耗时原样透出(响应模型用 int|float,不是 int)
        assert body["llm_calls"][0]["elapsed_ms"] == 12.5
        assert [list(item) for item in body["tools"]] == [TOOL_CALL_KEYS]
        assert body["tools"][0] == {"name": "validate_sql", "node": "gen_sql"}

    async def test_missing_run_404_detail_only(self, client):
        resp = await client.get("/v1/runs/nope")

        assert resp.status_code == 404
        assert list(resp.json()) == ["detail"]
