"""SessionManager tests (LangGraph era).

ask() returns the final WorkflowState; ask_stream() emits graph-native
events whose payloads carry the node name.
"""

import pytest

from trove.core.types import Message
from trove.workflow.state import WorkflowState


class TestSessionLifecycle:
    async def test_start_session(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        assert session.session_id
        assert session.project_name == "p1"

    async def test_start_session_same_project_same_name(self, session_manager):
        s1 = await session_manager.start_session(project_cwd="/tmp/p1")
        s2 = await session_manager.start_session(project_cwd="/tmp/p1")
        assert s1.project_name == s2.project_name == "p1"
        assert s1.session_id != s2.session_id

    async def test_save_and_load_session(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        session.messages.append(Message(role="user", content="hi"))
        await session_manager.save_session(session)

        loaded = await session_manager.load_session(session.session_id, "/tmp/p1")
        assert loaded.messages[0].content == "hi"

    async def test_list_sessions(self, session_manager):
        await session_manager.start_session(project_cwd="/tmp/p1")
        await session_manager.start_session(project_cwd="/tmp/p1")
        sessions = await session_manager.list_sessions("/tmp/p1")
        assert len(sessions) == 2

    async def test_delete_session(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        assert await session_manager.delete_session(session.session_id, "/tmp/p1") is True
        assert await session_manager.delete_session(session.session_id, "/tmp/p1") is False


class TestAsk:
    async def test_ask_returns_final_state(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        state = await session_manager.ask(
            session=session,
            question="What students are in Alameda county?",
            workflow_name="reflection",
        )
        assert isinstance(state, WorkflowState)
        assert state.final_response
        assert state.sql == "SELECT name FROM students;"
        assert state.row_count == 5
        assert state.verdict == "OK"
        assert state.error == ""

    async def test_ask_appends_messages(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        await session_manager.ask(
            session=session,
            question="question one",
            workflow_name="reflection",
        )
        assert len(session.messages) == 2  # user + assistant
        assert session.messages[0].role == "user"
        assert session.messages[1].role == "assistant"
        assert session.messages[1].metadata["workflow"] == "reflection"
        assert session.messages[1].metadata["sql"] == "SELECT name FROM students;"

    async def test_ask_persists(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        await session_manager.ask(session=session, question="q", workflow_name="reflection")

        loaded = await session_manager.load_session(session.session_id, "/tmp/p1")
        assert len(loaded.messages) == 2

    async def test_ask_empty_workflow(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        state = await session_manager.ask(
            session=session,
            question="hello",
            workflow_name="empty",
        )
        assert "(No query executed)" in state.final_response

    async def test_ask_unknown_workflow_raises(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        with pytest.raises(KeyError):
            await session_manager.ask(session=session, question="q", workflow_name="nope")


class TestAskStream:
    async def test_stream_yields_graph_events(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        events = []
        async for event in session_manager.ask_stream(
            session=session,
            question="test question",
            workflow_name="reflection",
        ):
            events.append(event)

        types = [e["type"] for e in events]
        assert types[0] == "thought"
        assert types[-1] == "done"
        assert "sql" in types
        assert "result" in types
        # graph-native payloads carry the producing node
        sql_event = next(e for e in events if e["type"] == "sql")
        assert sql_event["node"] == "gen_sql"
        done_event = events[-1]
        assert done_event["summary"]["sql"] == "SELECT name FROM students;"

    async def test_stream_records_exchange(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        async for _ in session_manager.ask_stream(
            session=session, question="q", workflow_name="reflection",
        ):
            pass
        assert len(session.messages) == 2

    async def test_stream_unknown_workflow_emits_error(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        events = []
        async for event in session_manager.ask_stream(
            session=session,
            question="test",
            workflow_name="nonexistent_workflow",
        ):
            events.append(event)

        assert any(e["type"] == "error" for e in events)

    async def test_stream_degradation_emits_error_event(self, tmp_home):
        """Graceful degradation: error event replaces done, with the final state."""
        from trove.services.datasource.catalog import CatalogService
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs
        from trove.agent.session import SessionManager
        from trove.core.config import AgentConfig

        class ScriptedLLM:
            async def chat(self, model, messages, **kwargs):
                return "```sql\nSELEC * FROM students;\n```"  # always invalid

        config = AgentConfig(home=str(tmp_home), target="mock/model")
        services = GraphServices(llm=ScriptedLLM())
        manager = SessionManager(
            config=config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs=build_graphs(services),
            llm_gateway=ScriptedLLM(),
        )
        session = await manager.start_session(project_cwd="/tmp/p1")
        events = []
        async for event in manager.ask_stream(
            session=session, question="q", workflow_name="reflection",
        ):
            events.append(event)

        assert events[-1]["type"] == "error"
        assert "3 attempts" in events[-1]["summary"]["error"]
        assert events[-1]["summary"]["final_response"]
        # exchange still recorded with the graceful explanation
        assert session.messages[-1].content == events[-1]["summary"]["final_response"]


class TestConversationHistory:
    async def test_ask_injects_prior_exchange_into_state(self, tmp_home):
        """第二次提问时，图收到的初始 state.history 含上一轮问答。"""
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.agent.session import SessionManager
        from trove.workflow.state import WorkflowState

        captured = []

        class StubGraph:
            async def ainvoke(self, state, config=None):
                captured.append(state)
                return {**state.model_dump(), "final_response": "answer"}

        manager = SessionManager(
            config=AgentConfig(home=str(tmp_home)),
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs={"reflection": StubGraph()},
            llm_gateway=None,
        )
        session = await manager.start_session(project_cwd="/tmp/p")

        await manager.ask(session=session, question="第一问")
        await manager.ask(session=session, question="第二问")

        assert captured[0].history == ""  # 第一轮无历史
        assert "第一问" in captured[1].history
        assert "answer" in captured[1].history  # 含上一轮答案
        assert "第二问" not in captured[1].history  # 当前问题不混入历史


class TestCompaction:
    async def test_compact_short_session_noop(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        session.messages.append(Message(role="user", content="hi"))
        session.messages.append(Message(role="assistant", content="hello"))

        compacted = await session_manager.compact_session(session)
        # Too short to compact — unchanged
        assert len(compacted.messages) == 2

    async def test_compact_long_session(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        for i in range(4):
            session.messages.append(Message(role="user", content=f"q{i}"))
            session.messages.append(Message(role="assistant", content=f"a{i}"))

        compacted = await session_manager.compact_session(session, keep_recent=1)
        # summary + 2 recent messages
        assert len(compacted.messages) == 3
        assert compacted.messages[0].role == "system"
        assert compacted.messages[1].content == "q3"
        assert compacted.messages[2].content == "a3"


class TestTokenUsage:
    def test_get_context_usage(self, session_manager):
        session = type("S", (), {})()
        session.messages = [
            Message(role="user", content="hello world " * 10),
        ]
        usage = session_manager.get_context_usage(session)
        assert "token_count" in usage
        assert "usage_ratio" in usage
        assert usage["token_count"] > 0

    def test_should_compact_false_for_short(self, session_manager):
        session = type("S", (), {})()
        session.messages = [Message(role="user", content="short")]
        assert session_manager.should_compact(session) is False

    def test_should_compact_true_for_long(self, session_manager):
        session = type("S", (), {})()
        # ~1M chars ≈ 200k+ tokens, well above 90% of 128k context
        long_content = "word " * 200000
        session.messages = [Message(role="user", content=long_content)]
        assert session_manager.should_compact(session) is True
