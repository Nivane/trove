"""Session store persistence tests."""

import pytest

from trove.core.types import Message
from trove.core.errors import SessionError
from trove.storage.session_store import SessionStore, _normalize_project_name


class TestNormalizeProjectName:
    def test_simple_path(self):
        assert _normalize_project_name("/home/user/my_project") == "my_project"

    def test_path_with_special_chars(self):
        assert _normalize_project_name("/home/user/my project") == "my_project"

    def test_empty_path(self):
        assert _normalize_project_name("/") == "default"

    def test_long_path_gets_md5_suffix(self):
        name = _normalize_project_name("/very/long/path/" + "a" * 60)
        assert len(name) <= 39  # 30 chars + _ + 8 md5 chars
        assert "_" in name


class TestCreateAndLoadSession:
    async def test_create_session(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/project1")
        assert session.session_id
        assert session.project_name == "project1"
        assert session.messages == []

    async def test_load_session(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        created = await store.create_session(project_cwd="/tmp/project1")
        loaded = await store.load_session(created.session_id, "/tmp/project1")
        assert loaded.session_id == created.session_id
        assert loaded.project_name == "project1"

    async def test_load_nonexistent_session_raises(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        with pytest.raises(SessionError):
            await store.load_session("nonexistent-id", "/tmp/project1")

    async def test_sessions_isolated_by_project(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        s1 = await store.create_session(project_cwd="/tmp/project_a")
        s2 = await store.create_session(project_cwd="/tmp/project_b")

        # Same session_id shouldn't collide because projects differ
        assert s1.project_name == "project_a"
        assert s2.project_name == "project_b"

        # Loading s1's id from project_b should fail
        with pytest.raises(SessionError):
            await store.load_session(s1.session_id, "/tmp/project_b")


class TestSaveSession:
    async def test_save_appends_messages(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/p")

        session.messages.append(Message(role="user", content="q1"))
        session.messages.append(Message(role="assistant", content="a1"))
        await store.save_session(session)

        loaded = await store.load_session(session.session_id, "/tmp/p")
        assert len(loaded.messages) == 2
        assert loaded.messages[0].content == "q1"
        assert loaded.messages[1].content == "a1"

    async def test_save_twice_no_duplicates(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/p")

        session.messages.append(Message(role="user", content="q1"))
        await store.save_session(session)

        session.messages.append(Message(role="assistant", content="a1"))
        await store.save_session(session)

        loaded = await store.load_session(session.session_id, "/tmp/p")
        assert len(loaded.messages) == 2  # not 3 (no duplicate of q1)

    async def test_message_metadata_roundtrip(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/p")

        session.messages.append(Message(
            role="assistant",
            content="result",
            metadata={"sql": "SELECT 1", "token_usage": 150},
        ))
        await store.save_session(session)

        loaded = await store.load_session(session.session_id, "/tmp/p")
        assert loaded.messages[0].metadata["sql"] == "SELECT 1"
        assert loaded.messages[0].metadata["token_usage"] == 150


class TestDeleteAndList:
    async def test_delete_session(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/p")

        assert await store.delete_session(session.session_id, "/tmp/p") is True
        assert await store.delete_session(session.session_id, "/tmp/p") is False

        with pytest.raises(SessionError):
            await store.load_session(session.session_id, "/tmp/p")

    async def test_list_sessions(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        await store.create_session(project_cwd="/tmp/p")
        await store.create_session(project_cwd="/tmp/p")

        sessions = await store.list_sessions("/tmp/p")
        assert len(sessions) == 2
        assert all("session_id" in s for s in sessions)
        assert all("message_count" in s for s in sessions)

    async def test_list_empty_project(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        sessions = await store.list_sessions("/tmp/empty_project")
        assert sessions == []


class TestCompactSession:
    async def test_compact_replaces_old_messages(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/p")

        # 6 messages = 3 user/assistant pairs
        for i in range(3):
            session.messages.append(Message(role="user", content=f"q{i}"))
            session.messages.append(Message(role="assistant", content=f"a{i}"))
        await store.save_session(session)

        compacted = await store.compact_session(
            session,
            summary_text="User asked about student grades",
            keep_recent=1,
        )

        # 1 summary + 2 recent messages
        assert len(compacted.messages) == 3
        assert compacted.messages[0].role == "system"
        assert "student grades" in compacted.messages[0].content
        assert compacted.summary == "User asked about student grades"

    async def test_compact_persists(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/p")

        for i in range(2):
            session.messages.append(Message(role="user", content=f"q{i}"))
            session.messages.append(Message(role="assistant", content=f"a{i}"))
        await store.save_session(session)

        await store.compact_session(session, "summary text", keep_recent=1)

        loaded = await store.load_session(session.session_id, "/tmp/p")
        assert loaded.summary == "summary text"
        assert loaded.messages[0].role == "system"
