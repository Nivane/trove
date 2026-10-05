"""Session store persistence tests."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

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

    async def test_metadata_with_db_typed_values_still_saves(self, tmp_home):
        """metadata 混进 DB 原生类型时降级成字符串,而不是抛。

        2026-10-05 归因答案「流中断」的落库侧回归门:当时 metadata 里是
        ``summary.analysis.evidence.queries[].rows`` 的 ``Decimal``/``date``
        (MySQL 驱动原样交回),``json.dumps`` 抛 TypeError → 生成器死在
        ``done`` 之前,答案既不送达也不落库。记录端现已安全化
        (``core.serialize``),这里的 ``default=str`` 只兜没预见的类型 ——
        既有约定同 ``sse.py``。
        """
        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/p")

        session.messages.append(Message(
            role="assistant",
            content="a",
            metadata={"summary": {"analysis": {"evidence": {"queries": [
                {"id": 1, "rows": [[Decimal("1.5"), date(2024, 1, 31)]]},
            ]}}}},
        ))
        await store.save_session(session)  # 不抛 = 门

        loaded = await store.load_session(session.session_id, "/tmp/p")
        assert len(loaded.messages) == 1
        assert loaded.messages[0].metadata["summary"]["analysis"]["evidence"]["queries"][0]["id"] == 1


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


class TestPinnedSessions:
    """置顶:meta KV 持久化 + 列表排序(置顶在前,组内 updated_at desc)。"""

    async def test_set_pinned_roundtrip(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/p")

        assert await store.set_pinned(session.session_id, True, "/tmp/p") is True
        rows = await store.list_sessions("/tmp/p")
        assert rows[0]["pinned"] is True

        assert await store.set_pinned(session.session_id, False, "/tmp/p") is True
        rows = await store.list_sessions("/tmp/p")
        assert rows[0]["pinned"] is False

    async def test_set_pinned_missing_session(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        assert await store.set_pinned("nope", True, "/tmp/p") is False

    async def test_pinned_sorts_first_across_pages(self, tmp_home):
        """排序必须在存储层:置顶最早的会话,首页(limit=1)仍是它。"""
        store = SessionStore(home_dir=str(tmp_home))
        p = _normalize_project_name("/tmp/p")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        s1 = await store.create_session(project_cwd="/tmp/p")
        s2 = await store.create_session(project_cwd="/tmp/p")
        s3 = await store.create_session(project_cwd="/tmp/p")
        await store.set_updated_at(p, s1.session_id, base)
        await store.set_updated_at(p, s2.session_id, base + timedelta(minutes=1))
        await store.set_updated_at(p, s3.session_id, base + timedelta(minutes=2))

        # 未置顶:updated_at desc = s3, s2, s1
        order = [r["session_id"] for r in await store.list_sessions("/tmp/p")]
        assert order == [s3.session_id, s2.session_id, s1.session_id]

        # 置顶最早的 s1 → 全局第一页第一行
        assert await store.set_pinned(s1.session_id, True, "/tmp/p") is True
        page1 = await store.list_sessions("/tmp/p", offset=0, limit=1)
        assert [r["session_id"] for r in page1] == [s1.session_id]
        # 第二页不再出现它(切片发生在排序之后)
        page2 = await store.list_sessions("/tmp/p", offset=1, limit=2)
        assert [r["session_id"] for r in page2] == [s3.session_id, s2.session_id]

    async def test_pin_does_not_bump_updated_at(self, tmp_home):
        """置顶改的是排序档位,不是会话活跃时间。"""
        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/p")
        before = (await store.load_session(session.session_id, "/tmp/p")).updated_at
        await store.set_pinned(session.session_id, True, "/tmp/p")
        after = (await store.load_session(session.session_id, "/tmp/p")).updated_at
        assert after == before


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


class TestClearSession:
    async def test_clear_removes_messages_and_summary(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/p")

        for i in range(3):
            session.messages.append(Message(role="user", content=f"q{i}"))
            session.messages.append(Message(role="assistant", content=f"a{i}"))
        session.summary = "old summary"
        await store.save_session(session)

        cleared = await store.clear_session(session)
        assert cleared.messages == []
        assert cleared.summary is None

        loaded = await store.load_session(session.session_id, "/tmp/p")
        assert loaded.messages == []
        assert loaded.summary is None

    async def test_clear_keeps_session_record(self, tmp_home):
        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/p")
        session.messages.append(Message(role="user", content="hi"))
        await store.save_session(session)

        await store.clear_session(session)
        # 会话仍可加载/继续使用,只是没有消息
        loaded = await store.load_session(session.session_id, "/tmp/p")
        assert loaded.messages == []
        loaded.messages.append(Message(role="user", content="again"))
        await store.save_session(loaded)
        assert len((await store.load_session(session.session_id, "/tmp/p")).messages) == 1


async def _seed_session(store: "SessionStore", project: str, user: str, n_msgs: int = 2) -> str:
    """Create a session with n_msgs messages in the given project."""
    session = await store.create_session(project, user_id=user)
    session.messages = [Message(role="user", content=f"q{i}", timestamp=datetime.now(timezone.utc), metadata={}) for i in range(n_msgs)]
    await store.save_session(session)
    return session.session_id


async def test_list_all_cross_project(tmp_home):
    """list_all 跨 project 汇总,含 size_bytes,按 updated_at 降序。"""
    from trove.storage.session_store import SessionStore

    store = SessionStore(home_dir=str(tmp_home))
    sid_a = await _seed_session(store, "proj_a", "alice")
    sid_b = await _seed_session(store, "proj_b", "bob")
    await _seed_session(store, "proj_b", "alice")

    all_sessions = await store.list_all()
    assert len(all_sessions) == 3
    by_id = {s["session_id"]: s for s in all_sessions}
    assert by_id[sid_a]["project_name"] == "proj_a"
    assert by_id[sid_b]["project_name"] == "proj_b"
    assert all("size_bytes" in s for s in all_sessions)  # 单库多表模型无独立文件体积
    assert all("user_id" in s and "updated_at" in s for s in all_sessions)
    # 降序:第一项 updated_at >= 第二项
    from datetime import datetime as dt
    times = [dt.fromisoformat(s["updated_at"]) for s in all_sessions]
    assert times == sorted(times, reverse=True)


async def test_list_all_filter_user(tmp_home):
    """user_id 过滤只返回该用户的会话。"""
    from trove.storage.session_store import SessionStore

    store = SessionStore(home_dir=str(tmp_home))
    await _seed_session(store, "proj_a", "alice")
    await _seed_session(store, "proj_a", "bob")
    sessions = await store.list_all(user_id="alice")
    assert len(sessions) == 1
    assert sessions[0]["user_id"] == "alice"
