"""MemoryService facade tests — observe / retrieve / store / promote / profile."""

from __future__ import annotations

import pytest

from trove.services.kb.service import KbService
from trove.services.memory.models import MemoryConfig, MemoryScope
from trove.services.memory.service import MemoryService
from trove.services.user_facts.service import UserFactsService


class _CannedLLM:
    """Scripted LLM gateway for failure-lesson distillation."""

    def __init__(self, response="{\"pattern\": \"loan\", \"note\": \"filter approved\"}"):
        self.response = response
        self.calls = 0

    async def chat(self, model, messages, **kwargs):
        self.calls += 1
        return self.response


@pytest.fixture
def kb(tmp_path):
    return KbService(tmp_path / "proj")


@pytest.fixture
def kb_dir(kb):
    kb.kb_dir.mkdir(parents=True)
    return kb.kb_dir


@pytest.fixture
def facts(tmp_path):
    return UserFactsService(tmp_path / "user_facts.db")


@pytest.fixture
def memory(tmp_path, kb, facts, monkeypatch):
    cfg = MemoryConfig(enabled=True, promotion=True, promotion_threshold=0.8)
    m = MemoryService(tmp_path / "home", cfg, kb=kb, user_facts=facts)
    return m


SCOPE = MemoryScope(datasource="demo", user_id="alice")


async def test_observe_records_episode(memory):
    await memory.observe(
        scope=SCOPE, question="avg loan by region", sql="SELECT region, AVG(amount)",
        verdict="OK", row_count=5, dialect="sqlite",
    )
    hits = await memory.episodes.search(SCOPE, "avg loan by region", limit=5)
    assert len(hits) == 1
    assert hits[0].content["sql"] == "SELECT region, AVG(amount)"
    assert hits[0].content["verdict"] == "OK"


async def test_observe_success_drafts_pending_example(memory, kb_dir):
    await memory.observe(
        scope=SCOPE, question="count of loans", sql="SELECT COUNT(*) FROM loan",
        verdict="OK", row_count=10, dialect="sqlite",
    )
    pending = await memory.kb.list_pending_examples("demo")
    assert any(e.get("tags") == ["auto"] for e in pending)


async def test_observe_correction_captures_pending_lesson(memory, kb_dir):
    await memory.observe(
        scope=SCOPE, question="q", sql="SELECT 1",
        verdict="OK", correction_history=["F2: filter missing approved status"],
    )
    lessons = await memory.kb.list_lessons("demo", confirmed_only=False)
    assert any("approved" in (l.get("pattern") or "") for l in lessons)


async def test_observe_failure_distills_lesson(memory, kb_dir):
    llm = _CannedLLM()
    memory.llm = llm
    await memory.observe(
        scope=SCOPE, question="how many loans per district",
        sql="SELECT 1", verdict="", error="execution failed", evidence="hint",
    )
    lessons = await memory.kb.list_lessons("demo", confirmed_only=False)
    assert len(lessons) >= 1
    assert lessons[0]["source"] == "auto_failure"


async def test_retrieve_dispatch_episodes(memory):
    await memory.observe(
        scope=SCOPE, question="sales by month", sql="SELECT month, SUM(x)",
        verdict="OK", row_count=3,
    )
    entries = await memory.retrieve(SCOPE, "sales by month", kinds=["episode"], limit=3)
    assert len(entries) == 1
    assert entries[0].kind == "episode"


# ── ⑦ touch 接线(读路径生命周期信号) ─────────────────────


async def test_episode_search_sets_idempotency_key(memory):
    """\x1f 契约:命中条目的 idempotency_key = "question\x1fsql"(touch 可拆)。"""
    await memory.observe(
        scope=SCOPE, question="sales by month", sql="SELECT month, SUM(x)",
        verdict="OK", row_count=3,
    )
    hits = await memory.episodes.search(SCOPE, "sales by month", limit=5)
    assert hits[0].idempotency_key == "sales by month\x1fSELECT month, SUM(x)"


async def test_retrieve_touches_read_hits(memory):
    """⑦ 接线:retrieve 命中即经 touch 刷新最近使用时间(生命周期信号)。"""
    await memory.observe(
        scope=SCOPE, question="sales by month", sql="SELECT month, SUM(x)",
        verdict="OK", row_count=3,
    )
    calls: list[tuple[str, str, str]] = []

    async def _spy(scope, question, sql=""):
        calls.append((scope.user_id, question, sql))

    memory.episodes.touch = _spy  # noqa: instance spy
    entries = await memory.retrieve(
        SCOPE, "sales by month", kinds=["episode"], limit=3)
    assert len(entries) == 1
    # 契约为 (user, question, sql) —— touch 按 \x1f 拆键后原样透传
    assert calls == [("alice", "sales by month", "SELECT month, SUM(x)")]


async def test_touch_refreshes_updated_at(memory):
    """touch 真实行为:同键更新 updated_at(未命中/拆键失败静默不炸)。"""
    import asyncio

    await memory.observe(
        scope=SCOPE, question="q", sql="SELECT 1", verdict="OK", row_count=1)
    await asyncio.sleep(0.02)
    before = (await memory.episodes.search(SCOPE, "q", limit=5))[0].updated_at
    await memory.touch(SCOPE, "episode", "q\x1fSELECT 1")
    after = (await memory.episodes.search(SCOPE, "q", limit=5))[0].updated_at
    assert after > before
    # 无 \x1f 的键 → 拆键 ValueError → 吞掉(读路径永不因 touch 失败阻塞)
    await memory.touch(SCOPE, "episode", "no-separator-key")


async def test_store_preference_draft(memory):
    entry = await memory.store(
        SCOPE, {"fact": "use 30-day average", "evidence": "user said so"},
        kind="preference", source="auto", confidence=0.7,
    )
    assert entry is not None
    drafts = await memory.preferences.list_pending(SCOPE)
    assert len(drafts) == 1
    assert drafts[0]["fact"] == "use 30-day average"


async def test_promote_lesson_bumps_confidence(memory, kb_dir):
    # 先写入一条 pending lesson(带 confidence)
    await memory.kb.append_lesson(
        {"pattern": "F2: filter missing", "note": "n", "confidence": 0.5,
         "source": "correction"},
        "demo",
    )
    # 再 promote 一次(evidence kind=upvote, +0.4 → 0.9 ≥ 0.8 阈值 → 自动确认)
    result = await memory.promote_lesson(
        "demo", "F2: filter missing", evidence_kind="upvote")
    assert result["updated"] is True
    assert result["promoted"] is True
    confirmed = await memory.kb.list_lessons("demo", confirmed_only=True)
    assert any("F2: filter missing" in (l.get("pattern") or "") for l in confirmed)


async def test_promote_lesson_disabled_without_config(memory, kb_dir):
    memory.config = MemoryConfig(enabled=True, promotion=False)
    await memory.kb.append_lesson({"pattern": "P1", "note": "n"}, "demo")
    result = await memory.promote_lesson("demo", "P1", evidence_kind="upvote")
    assert result["promoted"] is False


async def test_profile_aggregates(memory):
    await memory.observe(scope=SCOPE, question="q1", sql="SELECT 1",
                         verdict="OK", row_count=1)
    await memory.observe(scope=SCOPE, question="q2", sql="SELECT 2",
                         verdict="", row_count=-1,
                         correction_history=["wrong table"])
    prof = await memory.profile("alice", "demo")
    assert prof["totals"]["total"] == 2
    assert prof["totals"]["ok"] == 1
    assert prof["ok_rate"] == 0.5
    assert prof["failure_patterns"]


async def test_observe_disabled_when_config_off(memory):
    memory.config = MemoryConfig(enabled=False)
    await memory.observe(scope=SCOPE, question="q", sql="SELECT 1", verdict="OK")
    assert await memory.episodes.count(SCOPE) == 0
