"""Unified StorageBackend on real PostgreSQL — internal-state stores end-to-end.

Env-gated integration test (skipped unless PG_TEST_URL is set):

    PG_TEST_URL=postgresql://trove:trove@localhost:5432/trove \
        uv run pytest -m integration tests/storage/test_pg_storage.py

Exercises the unified storage layer (trove/storage/backends) with Postgres as
the production backend: user facts, settings, sessions/tasks, memory episodes,
and the LangGraph checkpointer all round-trip on the same PG instance.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

PG_URL = os.environ.get("PG_TEST_URL")

# CI 用 `-m integration` 选择此文件(PG 需真实服务;未设 PG_TEST_URL 自动跳过)。
pytestmark = pytest.mark.integration


def _skip_without_pg():
    if not PG_URL:
        pytest.skip("PG_TEST_URL not set")


@pytest.fixture
def storage_url() -> str:
    _skip_without_pg()
    return PG_URL


async def test_user_facts_and_settings_on_pg(storage_url, monkeypatch, tmp_path):
    from trove.services.admin_settings.store import SettingsStore
    from trove.services.user_facts.service import UserFactsService

    monkeypatch.setenv("TROVE_STORAGE_URL", storage_url)

    uf = UserFactsService(tmp_path / "facts.db")
    await uf.add("alice", "demo", "use 30-day average")
    facts = await uf.list("alice", "demo")
    assert any("30-day" in f["fact"] for f in facts)

    ss = SettingsStore(tmp_path / "settings.db")
    await ss.put_many({"llm.target": "mock/model", "nested": {"a": 1}})
    assert await ss.get("llm.target") == "mock/model"
    assert await ss.get("nested") == {"a": 1}


async def test_sessions_and_tasks_on_pg(storage_url, monkeypatch, tmp_path):
    from trove.core.types import Message
    from trove.storage.session_store import SessionStore
    from trove.storage.task_store import TaskStore

    monkeypatch.setenv("TROVE_STORAGE_URL", storage_url)
    store = SessionStore(home_dir=str(tmp_path / "home"))
    session = await store.create_session("/tmp/p", user_id="alice")
    session.messages.append(Message(role="user", content="hi"))
    await store.save_session(session)

    loaded = await store.load_session(session.session_id, "/tmp/p")
    assert loaded.user_id == "alice"
    assert len(loaded.messages) == 1

    tasks = TaskStore(store.backend(), session.project_name, session.session_id)
    from trove.core.types import Task

    await tasks.save_task(Task(
        title="任务", status="pending", position=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    ))
    assert len(await tasks.load_tasks()) == 1

    listed = await store.list_sessions("/tmp/p")
    assert any(s["session_id"] == session.session_id for s in listed)

    await store.delete_session(session.session_id, "/tmp/p")
    assert await tasks.load_tasks() == []  # 会话删除级联删任务
    await store.dispose()


async def test_memory_episodes_on_pg(storage_url, monkeypatch, tmp_path):
    from trove.services.memory.models import MemoryConfig, MemoryScope
    from trove.services.memory.service import MemoryService

    monkeypatch.setenv("TROVE_STORAGE_URL", storage_url)
    mem = MemoryService(tmp_path / "mhome", MemoryConfig(enabled=True))
    scope = MemoryScope(datasource="demo", user_id="alice")
    await mem.observe(
        scope=scope, question="avg loan by region", sql="SELECT region, AVG(amount)",
        verdict="OK", row_count=5,
    )
    hits = await mem.retrieve(scope, "avg loan by region", kinds=["episode"], limit=5)
    assert len(hits) == 1
    assert hits[0].content["verdict"] == "OK"


async def test_checkpointer_on_pg(storage_url, monkeypatch, tmp_path):
    """LangGraph checkpointer 在 PG 上全链路(真实图执行 + 跨轮续跑)。"""
    from langgraph.graph import END, START, StateGraph
    from typing import TypedDict

    from trove.storage.checkpoint_store import build_checkpointer

    monkeypatch.setenv("TROVE_STORAGE_URL", storage_url)

    class S(TypedDict):
        x: int

    def bump(state):
        return {"x": state["x"] + 1}

    g = StateGraph(S)
    g.add_node("b", bump)
    g.add_edge(START, "b")
    g.add_edge("b", END)
    app = g.compile()

    async with build_checkpointer(str(tmp_path / "home")) as ckpt:
        r1 = await app.ainvoke(
            {"x": 1}, {"configurable": {"thread_id": "pg-t1"}, "checkpoint": ckpt})
        r2 = await app.ainvoke(
            {"x": 10}, {"configurable": {"thread_id": "pg-t1"}, "checkpoint": ckpt})
        assert r1 == {"x": 2}
        assert r2 == {"x": 11}  # 续跑(thread_id 复用)生效
        await ckpt.adelete_thread("pg-t1")
