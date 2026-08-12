"""SessionManager tests."""

import pytest

from trove.core.types import Message


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
    async def test_ask_returns_response(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        response, result = await session_manager.ask(
            session=session,
            question="What students are in Alameda county?",
            workflow_name="reflection",
        )
        assert response  # non-empty response
        assert result.workflow_name == "reflection"
        assert len(result.nodes) > 0

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

    async def test_ask_persists(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        await session_manager.ask(session=session, question="q", workflow_name="reflection")

        loaded = await session_manager.load_session(session.session_id, "/tmp/p1")
        assert len(loaded.messages) == 2

    async def test_ask_empty_workflow(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        response, result = await session_manager.ask(
            session=session,
            question="hello",
            workflow_name="empty",
        )
        assert result.workflow_name == "empty"

    async def test_ask_unknown_workflow_raises(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        with pytest.raises(KeyError):
            await session_manager.ask(session=session, question="q", workflow_name="nope")


class TestAskStream:
    async def test_stream_yields_events(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        events = []
        async for event in session_manager.ask_stream(
            session=session,
            question="test question",
            workflow_name="reflection",
        ):
            events.append(event)

        assert events[0]["type"] == "thought"
        assert events[-1]["type"] == "done"

    async def test_stream_error_handling(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        events = []
        async for event in session_manager.ask_stream(
            session=session,
            question="test",
            workflow_name="nonexistent_workflow",
        ):
            events.append(event)

        # Error event present
        assert any(e["type"] == "error" for e in events)


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
