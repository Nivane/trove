"""End-to-end: SessionManager wires the memory facade through _record_exchange.

Verifies the P1+P2 wiring: after a successful ask, an episode lands in the
memory store and a pending auto-example is drafted — without breaking the
answer path (memory failures must never affect the response).
"""

from __future__ import annotations

import pytest

from trove.services.memory.models import MemoryConfig, MemoryScope
from trove.services.memory.service import MemoryService


class StubGraph:
    async def ainvoke(self, state, config=None):
        base = state if isinstance(state, dict) else state.model_dump()
        return {
            **base,
            "sql": "SELECT region, AVG(amount) FROM loan GROUP BY region",
            "verdict": "OK",
            "row_count": 3,
            "error": "",
            "correction_history": [],
            "final_response": "answer",
        }


async def test_ask_records_episode_and_drafts_example(tmp_home, sqlite_registry):
    from trove.agent.session import SessionManager
    from trove.core.config import AgentConfig
    from trove.services.kb.service import KbService
    from trove.storage.session_store import SessionStore

    kb = KbService(tmp_home / "proj")
    memory = MemoryService(
        tmp_home / "home",
        MemoryConfig(enabled=True, episodes=True, auto_examples=True),
        kb=kb,
        connectors=sqlite_registry,
    )
    manager = SessionManager(
        config=AgentConfig(home=str(tmp_home)),
        session_store=SessionStore(home_dir=str(tmp_home)),
        graphs={"reflection": StubGraph()},
        llm_gateway=None,
        kb=kb,
        connectors=sqlite_registry,
        memory=memory,
    )
    session = await manager.start_session(project_cwd="/tmp/p", user_id="alice")
    await manager.ask(session=session, question="average loan amount by region")

    scope = MemoryScope(datasource=sqlite_registry.default_name, user_id="alice")
    hits = await memory.episodes.search(scope, "average loan amount by region", limit=5)
    assert len(hits) == 1
    assert hits[0].content["verdict"] == "OK"

    # 成功查询自动草拟 pending 示例(不进检索直到 admin 确认)
    pending = await kb.list_pending_examples(sqlite_registry.default_name)
    assert any("AVG(amount)" in (e.get("sql") or "") for e in pending)


async def test_ask_without_memory_still_records_lesson(tmp_home, sqlite_registry):
    """回归:未配置 memory 时,旧 _capture_lessons 路径(修正→pending lesson)保持。"""
    from trove.agent.session import SessionManager
    from trove.core.config import AgentConfig
    from trove.services.kb.service import KbService
    from trove.storage.session_store import SessionStore

    kb = KbService(tmp_home / "proj")
    kb.kb_dir.mkdir(parents=True)

    class _CorrGraph:
        async def ainvoke(self, state, config=None):
            base = state if isinstance(state, dict) else state.model_dump()
            return {
                **base,
                "sql": "SELECT * FROM loan", "error": "",
                "correction_history": ["no such table: loans"],
                "final_response": "answer",
            }

    manager = SessionManager(
        config=AgentConfig(home=str(tmp_home)),
        session_store=SessionStore(home_dir=str(tmp_home)),
        graphs={"reflection": _CorrGraph()},
        llm_gateway=None,
        kb=kb,
        connectors=sqlite_registry,
    )
    session = await manager.start_session(project_cwd="/tmp/p")
    await manager.ask(session=session, question="q")

    all_lessons = await kb.list_lessons(sqlite_registry.default_name, confirmed_only=False)
    assert any("loans" in (l.get("pattern") or "") for l in all_lessons)
