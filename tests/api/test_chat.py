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

    async def test_rename_session_title_roundtrip(self, client):
        created = (await client.post("/v1/sessions")).json()["session_id"]
        resp = await client.post(
            f"/v1/sessions/{created}/title", json={"title": "季度分析"}
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "季度分析"
        listing = (await client.get("/v1/sessions")).json()["sessions"]
        by_id = {s["session_id"]: s for s in listing}
        assert by_id[created]["title"] == "季度分析"
        foreign = await client.post(
            "/v1/sessions/~/nope/title", json={"title": "x"}
        )
        assert foreign.status_code == 404

    async def test_pin_session_roundtrip(self, client):
        created = (await client.post("/v1/sessions")).json()["session_id"]
        resp = await client.post(f"/v1/sessions/{created}/pin", json={"pinned": True})
        assert resp.status_code == 200
        assert resp.json() == {"session_id": created, "pinned": True}
        listing = (await client.get("/v1/sessions")).json()["sessions"]
        by_id = {s["session_id"]: s for s in listing}
        assert by_id[created]["pinned"] is True

        resp = await client.post(f"/v1/sessions/{created}/pin", json={"pinned": False})
        assert resp.status_code == 200
        assert resp.json()["pinned"] is False
        listing = (await client.get("/v1/sessions")).json()["sessions"]
        by_id = {s["session_id"]: s for s in listing}
        assert by_id[created]["pinned"] is False

    async def test_pin_sorts_first_across_pages(self, client):
        """置顶排最前,且分页切片发生在排序之后(首页 limit=1 就是它)。"""
        ids = []
        for _ in range(3):
            ids.append((await client.post("/v1/sessions")).json()["session_id"])
        oldest = ids[0]
        assert (
            await client.post(f"/v1/sessions/{oldest}/pin", json={"pinned": True})
        ).status_code == 200

        page1 = (await client.get("/v1/sessions", params={"limit": 1})).json()
        assert page1["sessions"][0]["session_id"] == oldest
        assert page1["sessions"][0]["pinned"] is True

        page2 = (
            await client.get("/v1/sessions", params={"limit": 2, "offset": 1})
        ).json()
        assert oldest not in [s["session_id"] for s in page2["sessions"]]

    async def test_pin_missing_session_404(self, client):
        resp = await client.post("/v1/sessions/nope/pin", json={"pinned": True})
        assert resp.status_code == 404

    async def test_list_sessions(self, client):
        assert (await client.post("/v1/sessions")).status_code == 201
        resp = await client.get("/v1/sessions")
        assert resp.status_code == 200
        assert isinstance(resp.json()["sessions"], list)

    async def test_list_sessions_pagination(self, client):
        for _ in range(3):
            assert (await client.post("/v1/sessions")).status_code == 201
        page = await client.get("/v1/sessions", params={"limit": 2, "offset": 0})
        assert page.status_code == 200
        body = page.json()
        assert len(body["sessions"]) == 2
        assert body["has_more"] is True
        next_page = await client.get(
            "/v1/sessions", params={"limit": 2, "offset": 2}
        )
        body2 = next_page.json()
        assert len(body2["sessions"]) == 1
        assert body2["has_more"] is False
        ids_page1 = {s["session_id"] for s in body["sessions"]}
        ids_page2 = {s["session_id"] for s in body2["sessions"]}
        assert not (ids_page1 & ids_page2)

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


class TestOnBehalfOf:
    """P5 / 设计 §5.6 —— ``POST /v1/chat {"on_behalf_of": "user:42"}``。

    重放是**读别人数据的通道**,所以校验全在**跑起来之前**:非 admin 403、
    目标不存在 404、形状不认识 400、不是自己的会话 403。四种错法都必须在
    SSE 开始之前挡掉 —— 流一旦开始,状态码已经发出去了,错误只能变成事件。
    """

    async def test_non_admin_is_forbidden(self, user_client, user_token, auth_service):
        bob = await auth_service.store.get_user_by_username("bob")
        resp = await user_client.post(
            "/v1/chat",
            json={"question": "hi", "on_behalf_of": f"user:{bob['id']}"},
        )
        assert resp.status_code == 403

    async def test_missing_target_is_404(self, client):
        resp = await client.post(
            "/v1/chat", json={"question": "hi", "on_behalf_of": "user:99999"},
        )
        assert resp.status_code == 404

    async def test_unknown_subject_type_is_400(self, client):
        """``group:3`` 不认识 —— 拒绝,不猜(不静默按自己的身份跑)。"""
        resp = await client.post(
            "/v1/chat", json={"question": "hi", "on_behalf_of": "group:3"},
        )
        assert resp.status_code == 400

    async def test_replay_in_a_foreign_session_is_403(self, client, user_client, auth_service):
        """重放只能在**自己的**会话里做。

        会话层按**会话主人**的 role 复核发起人(它拿不到别的身份),所以
        「admin 在别人的会话里重放」在两层的判定会打架 —— 与其让内层拒绝、
        外层放行,不如在门口就说清楚。
        """
        bob = await auth_service.store.get_user_by_username("bob")
        created = (await user_client.post("/v1/sessions")).json()["session_id"]
        resp = await client.post(
            "/v1/chat",
            json={
                "session_id": created, "question": "hi",
                "on_behalf_of": f"user:{bob['id']}",
            },
        )
        assert resp.status_code == 403

    async def test_admin_replay_streams_as_the_target(self, client, auth_service):
        """happy path:重放跑完,并在审计里留下「谁借了谁的身份」。"""
        bob = await auth_service.store.get_user_by_username("bob")
        created = (await client.post("/v1/sessions")).json()["session_id"]

        resp = await client.post(
            "/v1/chat",
            json={
                "session_id": created, "question": "What students are in Alameda county?",
                "on_behalf_of": f"user:{bob['id']}",
            },
        )

        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert events[-1][0] == "done"
        audit = await auth_service.list_audit(action="authz.on_behalf_of")
        assert len(audit) == 1
        details = audit[0]["details"]
        assert details["on_behalf_of"] == str(bob["id"])
        assert details["actor"] == "1"          # bootstrap admin

    async def test_plain_chat_is_unaffected(self, client):
        """不传 ``on_behalf_of`` 的老客户端行为一字不变。"""
        resp = await client.post("/v1/chat", json={"question": "hello"})
        assert resp.status_code == 200
        assert parse_sse(resp.text)[-1][0] == "done"


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
            GraphServices(llm=gateway, connectors=sqlite_registry,semantic_layer=getattr(sqlite_registry, "_test_semantic_provider", None), config=config),
            checkpointer=InMemorySaver(),
            multi_candidate=False, query_sketch=False, agentic=False,
        )
        manager = SessionManager(
            config=config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs=graphs,
            llm_gateway=gateway,
        )
        app = create_app(
            {"session_manager": manager, "connector_registry": sqlite_registry},
            allow_null_auth=True,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            session_id = (await c.post("/v1/sessions")).json()["session_id"]
            resp = await c.post(
                "/v1/chat",
                json={"session_id": session_id, "question": "students average grade by county"},
            )
            events = parse_sse(resp.text)
            types = [t for t, _ in events]
            assert types[-1] == "hitl"
            # payload 携带待确认 SQL
            payload = [d["payload"] for t, d in events if t == "hitl"][0]
            assert payload["kind"] == "confirm_sql"
            assert "SELECT name FROM students;" in payload["sql"]

            # 暂停即落盘(方案 ①⑤):提问 + 「等待确认」书签(pending)+ 已收集
            # 步骤已在历史里(GET 透出形状,防序列化丢字段)
            paused = (await c.get(f"/v1/sessions/{session_id}")).json()
            assert [m["role"] for m in paused["messages"]] == ["user", "assistant"]
            bookmark_meta = paused["messages"][1]["metadata"]
            pending_hitl = bookmark_meta["hitl"]
            assert pending_hitl["status"] == "pending"
            assert pending_hitl["run_id"]
            assert pending_hitl["workflow"] == "reflection"
            assert "SELECT name FROM students;" in bookmark_meta["sql"]
            steps = bookmark_meta["steps"]
            assert steps and steps[0]["type"] == "step"
            assert {"seq", "node", "elapsed_ms", "lang", "detail"} <= set(steps[0])

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

            # 会话落库:提问(⑤)+ 中断书签(②)+ 终答 = 3 条。书签是中断时的
            # 事实快照(消息表 append-only,status 保持 pending 不更新)——
            # 失效规则「只认最后一条」:答案消息落在其后,书签自然失效。
            detail = (await c.get(f"/v1/sessions/{session_id}")).json()
            msgs = detail["messages"]
            assert [m["role"] for m in msgs] == ["user", "assistant", "assistant"]
            assert msgs[1]["metadata"]["hitl"]["status"] == "pending"
            assert msgs[2]["metadata"]["row_count"] == 5

            # 书签已失效 → 再 resume 响亮报错(不静默续一条走完的线程)
            again = parse_sse(
                (await c.post(
                    f"/v1/sessions/{session_id}/resume", json={"decision": "yes"},
                )).text
            )
            assert [t for t, _ in again] == ["error"]
            assert "没有待确认" in again[0][1]["content"] or \
                "Nothing is pending" in again[0][1]["content"]

        await manager.dispose()


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
            GraphServices(llm=gateway, connectors=sqlite_registry,semantic_layer=getattr(sqlite_registry, "_test_semantic_provider", None), config=config),
            checkpointer=InMemorySaver(),
            multi_candidate=False, query_sketch=False, agentic=False,
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
        app = create_app(
            {"session_manager": manager, "connector_registry": sqlite_registry},
            allow_null_auth=True,
        )

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

        await manager.dispose()

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
        app = create_app(
            {"session_manager": manager, "connector_registry": sqlite_registry},
            allow_null_auth=True,
        )

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
            # 收尾 batched done 带聚合耗时(前端据此展示统计)
            assert resume_events[-1][1]["summary"]["total_elapsed_ms"] >= 0

            tasks = (await c.get(f"/v1/sessions/{session_id}/tasks")).json()["tasks"]
            assert [t["status"] for t in tasks] == ["done", "done"]

        await manager.dispose()


class TestChatDatasource:
    """/v1/chat datasource 参数:授权通过放行,未授权 403。"""

    async def test_admin_targets_named_datasource(self, client):
        resp = await client.post(
            "/v1/chat",
            json={"question": "Which county has most students?", "datasource": "test_db"},
        )
        assert resp.status_code == 200
        assert "session" in resp.text

    async def test_user_allowed_default_datasource(self, user_client):
        resp = await user_client.post(
            "/v1/chat",
            json={"question": "Which county has most students?", "datasource": "test_db"},
        )
        assert resp.status_code == 200

    async def test_user_forbidden_datasource_403(self, user_client):
        resp = await user_client.post(
            "/v1/chat", json={"question": "hello", "datasource": "other_db"}
        )
        assert resp.status_code == 403
        assert "not allowed" in resp.text
