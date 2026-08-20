"""Chat + session endpoint tests (SSE over the real reflection graph)."""

from __future__ import annotations

import json

from tests.conftest import ScriptedGateway


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse a text/event-stream body into [(event, data), ...]."""
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        etype = lines[0].split(":", 1)[1].strip()
        data = json.loads(lines[1].split(":", 1)[1])
        events.append((etype, data))
    return events


class TestSessions:
    async def test_create_session(self, client):
        resp = await client.post("/v1/sessions")
        assert resp.status_code == 201
        assert resp.json()["session_id"]

    async def test_create_and_get_session(self, client):
        created = (await client.post("/v1/sessions")).json()["session_id"]
        resp = await client.get(f"/v1/sessions/{created}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == created
        assert body["messages"] == []

    async def test_get_missing_session_404(self, client):
        resp = await client.get("/v1/sessions/nope")
        assert resp.status_code == 404

    async def test_delete_session(self, client):
        created = (await client.post("/v1/sessions")).json()["session_id"]
        assert (await client.delete(f"/v1/sessions/{created}")).status_code == 204
        assert (await client.get(f"/v1/sessions/{created}")).status_code == 404
        assert (await client.delete(f"/v1/sessions/{created}")).status_code == 404

    async def test_list_sessions(self, client):
        assert (await client.post("/v1/sessions")).status_code == 201
        resp = await client.get("/v1/sessions")
        assert resp.status_code == 200
        assert isinstance(resp.json()["sessions"], list)

    async def test_clear_session(self, client):
        created = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(
            "/v1/chat",
            json={"session_id": created, "question": "Which county has most students?"},
        )
        resp = await client.post(f"/v1/sessions/{created}/clear")
        assert resp.status_code == 200
        assert resp.json()["message_count"] == 0

        detail = (await client.get(f"/v1/sessions/{created}")).json()
        assert detail["messages"] == []
        assert detail["summary"] is None

    async def test_clear_missing_session_404(self, client):
        assert (await client.post("/v1/sessions/nope/clear")).status_code == 404

    async def test_compact_session(self, client, api_app):
        """压缩后返回摘要与剩余消息数(该会话 manager 的 LLM 只在压缩时被调用)。"""
        from trove.core.types import Message
        manager = api_app.state.session_manager
        created = (await client.post("/v1/sessions")).json()["session_id"]
        session = await manager.load_session(created)
        for i in range(4):
            session.messages.append(Message(role="user", content=f"q{i}"))
            session.messages.append(Message(role="assistant", content=f"a{i}"))
        await manager.save_session(session)

        resp = await client.post(f"/v1/sessions/{created}/compact")
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]
        assert body["message_count"] == 7  # summary + keep_recent=3 pairs

        detail = (await client.get(f"/v1/sessions/{created}")).json()
        assert detail["messages"][0]["role"] == "system"
        assert detail["summary"]

    async def test_compact_missing_session_404(self, client):
        assert (await client.post("/v1/sessions/nope/compact")).status_code == 404


class TestChat:
    async def test_chat_streams_typed_events(self, client):
        resp = await client.post(
            "/v1/chat",
            json={"question": "What students are in Alameda county?"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        events = parse_sse(resp.text)
        types = [t for t, _ in events]
        assert types[0] == "session"
        assert types[-1] == "done"
        assert "sql" in types
        assert "result" in types
        # the session event teaches the client the session id
        session_id = events[0][1]["session_id"]
        assert session_id
        assert events[-1][1]["summary"]["sql"] == "SELECT name FROM students;"

    async def test_chat_existing_session_continues(self, client):
        created = (await client.post("/v1/sessions")).json()["session_id"]
        resp = await client.post(
            "/v1/chat",
            json={"session_id": created, "question": "Which county has most students?"},
        )
        events = parse_sse(resp.text)
        assert events[0][1]["session_id"] == created

        detail = (await client.get(f"/v1/sessions/{created}")).json()
        assert len(detail["messages"]) == 2  # user + assistant recorded

    async def test_chat_missing_session_404(self, client):
        resp = await client.post(
            "/v1/chat",
            json={"session_id": "nope", "question": "hello"},
        )
        assert resp.status_code == 404

    async def test_chat_unknown_workflow_emits_error_event(self, client):
        resp = await client.post(
            "/v1/chat",
            json={"question": "hi", "workflow": "nonexistent"},
        )
        events = parse_sse(resp.text)
        assert events[-1][0] == "error"

    async def test_chat_empty_question_422(self, client):
        resp = await client.post("/v1/chat", json={"question": ""})
        assert resp.status_code == 422

    async def test_health(self, client):
        resp = await client.get("/v1/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestChatHITLResume:
    """HITL:chat 流发出 hitl 事件暂停;POST /resume 以决定继续。"""

    async def test_chat_hitl_pause_and_resume(self, tmp_home, sqlite_registry):
        from langgraph.checkpoint.memory import InMemorySaver
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs
        from trove.agent.session import SessionManager
        from trove.api.app import create_app
        from httpx import ASGITransport, AsyncClient

        class Gateway:
            def __init__(self, responses):
                self._responses = iter(responses)
                self.calls = []

            async def chat(self, model, messages, **kwargs):
                self.calls.append(kwargs.get("metadata", {}).get("node"))
                return next(self._responses)

            async def chat_full(self, model, messages, tools=None, **kwargs):
                self.calls.append(kwargs.get("metadata", {}).get("node"))
                return {"content": next(self._responses), "tool_calls": []}

        config = AgentConfig(
            home=str(tmp_home), target="mock/model",
            explain_semantics=True, hitl=True, insights=True,
        )
        gateway = Gateway([
            "query",
            "```sql\nSELECT name FROM students;\n```",
            "这条 SQL 查询学生姓名",
            "OK",
            "- 共 5 名学生",
        ])
        graphs = build_graphs(
            GraphServices(llm=gateway, connectors=sqlite_registry, config=config),
            checkpointer=InMemorySaver(),
            multi_candidate=False, planner=False, agentic=False,
        )
        manager = SessionManager(
            config=config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs=graphs,
            llm_gateway=gateway,
        )
        app = create_app({"session_manager": manager})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            session_id = (await c.post("/v1/sessions")).json()["session_id"]
            resp = await c.post(
                "/v1/chat",
                json={"session_id": session_id, "question": "What is the average loan amount?"},
            )
            events = parse_sse(resp.text)
            types = [t for t, _ in events]
            assert types[-1] == "hitl"
            # payload 携带待确认 SQL
            payload = [d["payload"] for t, d in events if t == "hitl"][0]
            assert payload["kind"] == "confirm_sql"
            assert "SELECT name FROM students;" in payload["sql"]

            # 批准 → SSE 事件流:done 终态带执行结果与洞察
            resume = await c.post(
                f"/v1/sessions/{session_id}/resume",
                json={"decision": "yes"},
            )
            assert resume.status_code == 200
            resume_events = parse_sse(resume.text)
            assert resume_events[-1][0] == "done"
            summary = resume_events[-1][1]["summary"]
            assert summary["hitl_status"] == "approved"
            assert summary["row_count"] == 5
            assert summary["insights"] == ["共 5 名学生"]
            assert summary["final_response"]

            # 会话落库为一次完整问答
            detail = (await c.get(f"/v1/sessions/{session_id}")).json()
            assert len(detail["messages"]) == 2


class TestChatTasks:
    """跨轮任务层 API:GET /tasks 快照 + 多任务流 + 批内 HITL 三选项。"""

    @staticmethod
    def _build_manager(tmp_home, sqlite_registry, responses, *, hitl=False):
        from langgraph.checkpoint.memory import InMemorySaver

        from trove.agent.session import SessionManager
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs

        config = AgentConfig(home=str(tmp_home), target="mock/model", hitl=hitl)
        gateway = ScriptedGateway(responses)
        graphs = build_graphs(
            GraphServices(llm=gateway, connectors=sqlite_registry, config=config),
            checkpointer=InMemorySaver(),
            multi_candidate=False, planner=False, agentic=False,
        )
        return SessionManager(
            config=config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs=graphs,
            llm_gateway=gateway,
        )

    async def test_tasks_endpoint_empty_for_fresh_session(self, client):
        created = (await client.post("/v1/sessions")).json()["session_id"]
        resp = await client.get(f"/v1/sessions/{created}/tasks")
        assert resp.status_code == 200
        assert resp.json() == {"session_id": created, "tasks": []}

    async def test_tasks_endpoint_404(self, client):
        assert (await client.get("/v1/sessions/nope/tasks")).status_code == 404

    async def test_chat_multitask_streams_and_persists_tasks(self, tmp_home, sqlite_registry):
        """多任务 chat:逐任务 done + 收尾 batched done;GET /tasks 返回持久化快照。"""
        from trove.api.app import create_app
        from httpx import ASGITransport, AsyncClient

        SQL = "```sql\nSELECT name FROM students;\n```"
        manager = self._build_manager(
            tmp_home, sqlite_registry,
            [
                '{"tasks": ["学生名单", "平均成绩"]}',
                "query", SQL, "OK",
                "query", SQL, "OK",
            ],
        )
        app = create_app({"session_manager": manager})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            session_id = (await c.post("/v1/sessions")).json()["session_id"]
            resp = await c.post(
                "/v1/chat",
                json={"session_id": session_id, "question": "分别查询 1. 学生名单 2. 平均成绩"},
            )
            events = parse_sse(resp.text)
            types = [t for t, _ in events]

            # task 快照 ×5(初始 + 每任务 in_progress/终态)+ 3 个 done(2 逐任务 + 1 收尾)
            assert types.count("task") == 5
            assert types.count("done") == 3
            done_data = [d for t, d in events if t == "done"]
            assert done_data[-1]["summary"]["batched"] is True
            assert "任务 1/2" in done_data[0]["content"]
            assert "任务 2/2" in done_data[1]["content"]

            # 持久化快照(会话文件 tasks 表)
            tasks = (await c.get(f"/v1/sessions/{session_id}/tasks")).json()["tasks"]
            assert [t["status"] for t in tasks] == ["done", "done"]
            assert [t["title"] for t in tasks] == ["学生名单", "平均成绩"]

            # 会话消息:user + 每任务 assistant(带 task_id 元数据)
            detail = (await c.get(f"/v1/sessions/{session_id}")).json()
            assert len(detail["messages"]) == 3
            assert all(m["metadata"]["task_id"] for m in detail["messages"] if m["role"] == "assistant")

    async def test_chat_batch_hitl_approve_all_resume_streams(self, tmp_home, sqlite_registry):
        """批内 HITL:hitl 事件带 task_context;approve_all resume 以 SSE 流收尾。"""
        from trove.api.app import create_app
        from httpx import ASGITransport, AsyncClient

        SQL = "```sql\nSELECT name FROM students;\n```"
        manager = self._build_manager(
            tmp_home, sqlite_registry,
            [
                '{"tasks": ["学生名单", "平均成绩"]}',
                "query", SQL,        # 任务1 → HITL 中断
                "OK",                # resume:reflect
                "query", SQL, "OK",  # 任务2:auto_approve
            ],
            hitl=True,
        )
        app = create_app({"session_manager": manager})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            session_id = (await c.post("/v1/sessions")).json()["session_id"]
            resp = await c.post(
                "/v1/chat",
                json={"session_id": session_id, "question": "分别查询 1. 学生名单 2. 平均成绩"},
            )
            events = parse_sse(resp.text)
            hitl_data = [d for t, d in events if t == "hitl"]
            assert hitl_data
            assert hitl_data[0]["payload"]["task_context"]["total"] == 2

            # approve_all:整个批次在此流中完成,收尾事件为 batched done
            resume = await c.post(
                f"/v1/sessions/{session_id}/resume",
                json={"decision": "approve_all"},
            )
            assert resume.status_code == 200
            resume_events = parse_sse(resume.text)
            resume_types = [t for t, _ in resume_events]
            assert resume_types.count("done") == 3
            assert resume_events[-1][1]["summary"]["batched"] is True

            tasks = (await c.get(f"/v1/sessions/{session_id}/tasks")).json()["tasks"]
            assert [t["status"] for t in tasks] == ["done", "done"]
