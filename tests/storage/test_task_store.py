"""TaskStore tests — cross-turn task persistence in the shared SessionStore backend.

Tasks share the SessionStore backend (single StorageBackend, rows keyed by
(project, session_id)); compaction must never delete the session row (tasks
survive; messages are rewritten in place). Deleting a session cascades to its
tasks (same backend).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trove.core.types import Task
from trove.storage.task_store import TaskStore


def _task(title: str, position: int, status: str = "pending") -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        title=title,
        status=status,
        position=position,
        created_at=now,
        updated_at=now,
    )


class TestTaskStoreCRUD:
    @pytest.fixture
    def store(self, tmp_path):
        from trove.storage.backends import resolve_backend
        from trove.storage.task_store import TaskStore

        backend = resolve_backend(str(tmp_path / "sessions" / "p" / "s1.sqlite"))
        return TaskStore(backend, "proj", "s1")

    async def test_save_and_load_ordered_by_position(self, store):
        await store.save_task(_task("任务一", 1))
        await store.save_task(_task("任务零", 0))
        await store.save_task(_task("任务二", 2))

        tasks = await store.load_tasks()
        assert [t.title for t in tasks] == ["任务零", "任务一", "任务二"]
        assert [t.position for t in tasks] == [0, 1, 2]
        assert all(t.status == "pending" for t in tasks)

    async def test_save_same_id_updates_in_place(self, store):
        t = _task("旧标题", 0)
        await store.save_task(t)
        t.title = "新标题"
        t.status = "done"
        await store.save_task(t)

        tasks = await store.load_tasks()
        assert len(tasks) == 1
        assert tasks[0].title == "新标题"
        assert tasks[0].status == "done"

    async def test_update_status_merges_metadata(self, store):
        t = _task("任务", 0)
        await store.save_task(t)
        updated = await store.update_status(
            t.task_id, "failed", {"error": "sql error"}
        )
        assert updated is not None
        assert updated.status == "failed"
        assert updated.metadata["error"] == "sql error"

        # 后续更新保留已合并的 metadata
        updated = await store.update_status(t.task_id, "done", {"run_id": "r1"})
        assert updated.metadata["error"] == "sql error"
        assert updated.metadata["run_id"] == "r1"

    async def test_update_status_unknown_id_returns_none(self, store):
        await store.save_task(_task("任务", 0))
        assert await store.update_status("nope", "done") is None

    async def test_clear_removes_all(self, store):
        await store.save_task(_task("任务一", 0))
        await store.save_task(_task("任务二", 1))
        await store.clear()
        assert await store.load_tasks() == []

    async def test_clear_idempotent(self, store):
        await store.clear()
        assert await store.load_tasks() == []


class TestTaskStoreSharesSessionBackend:
    """Tasks live on the same backend as messages — compaction/deletion coherence."""

    @pytest.fixture
    async def session_and_store(self, tmp_home):
        from trove.storage.session_store import SessionStore

        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/p")
        return session, store

    def _task_store(self, store, session):
        return TaskStore(store.backend(), session.project_name, session.session_id)

    async def test_task_survives_compact(self, tmp_home, session_and_store):
        session, store = session_and_store
        task_store = self._task_store(store, session)
        await task_store.save_task(_task("任务一", 0))
        await task_store.save_task(_task("任务二", 1))

        from trove.core.types import Message
        session.messages.append(Message(role="user", content="Q1"))
        session.messages.append(Message(role="assistant", content="A1"))
        session.messages.append(Message(role="user", content="Q2"))
        session.messages.append(Message(role="assistant", content="A2"))
        session.messages.append(Message(role="user", content="Q3"))
        session.messages.append(Message(role="assistant", content="A3"))

        compacted = await store.compact_session(session, "summary text", keep_recent=2)
        assert len(compacted.messages) == 5  # summary + 最近 2 轮(user+assistant)

        # 同一 backend —— 会话行未删除,任务表保留
        tasks = await task_store.load_tasks()
        assert [t.title for t in tasks] == ["任务一", "任务二"]
        assert tasks[0].status == "pending"

    async def test_compact_keeps_meta_keys(self, tmp_home):
        """compact 改为原地重写后,meta 的 user_id 等键不得丢失。"""
        from trove.storage.session_store import SessionStore

        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/p", user_id="u-42")

        await store.compact_session(session, "summary text", keep_recent=1)

        loaded = await store.load_session(session.session_id, project_cwd="/tmp/p")
        assert loaded.user_id == "u-42"
        assert loaded.summary == "summary text"

    async def test_delete_session_removes_tasks(self, tmp_home, session_and_store):
        session, store = session_and_store
        task_store = self._task_store(store, session)
        await task_store.save_task(_task("任务一", 0))

        await store.delete_session(session.session_id, project_cwd="/tmp/p")
        await store.delete_session(session.session_id, project_cwd="/tmp/p")  # 幂等

        # 会话删除级联删任务(同 backend);新 TaskStore 读同库 → 空
        fresh = TaskStore(store.backend(), session.project_name, session.session_id)
        assert await fresh.load_tasks() == []
