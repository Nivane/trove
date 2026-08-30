"""EpisodeStore (episodic memory) tests — in-memory SQLite, zero network."""

from __future__ import annotations

import pytest

from trove.services.memory.episode import EpisodeStore
from trove.services.memory.models import MemoryScope


@pytest.fixture
async def store(tmp_path):
    return EpisodeStore(tmp_path / "memory" / "episodes.sqlite")


async def test_record_and_count(store):
    scope = MemoryScope(datasource="demo", user_id="alice")
    await store.record(scope, question="avg loan by region", sql="SELECT 1",
                       verdict="OK", row_count=5)
    assert await store.count(scope) == 1
    assert await store.count(MemoryScope(datasource="other")) == 0


async def test_record_idempotent_dedup(store):
    scope = MemoryScope(datasource="demo", user_id="alice")
    await store.record(scope, question="q", sql="SELECT 1", verdict="OK")
    await store.record(scope, question="q", sql="SELECT 1", verdict="EMPTY")
    assert await store.count(scope) == 1  # 同 scope+question+sql → 刷新不重复


async def test_search_relevance_gate(store):
    scope = MemoryScope(datasource="demo", user_id="alice")
    await store.record(scope, question="average loan amount by region",
                       sql="SELECT region, AVG(amount)", verdict="OK")
    await store.record(scope, question="totally unrelated question about weather",
                       sql="SELECT 2", verdict="OK")
    hits = await store.search(scope, "average loan amount by region", limit=5)
    # 确定性门:不相关 episode 不返回;相关返回且带 score
    assert len(hits) >= 1
    assert all(h.score > 0 for h in hits)
    assert hits[0].content["question"] == "average loan amount by region"
    assert hits[0].kind == "episode"


async def test_search_scoped_by_user(store):
    alice = MemoryScope(datasource="demo", user_id="alice")
    bob = MemoryScope(datasource="demo", user_id="bob")
    await store.record(alice, question="sales by month", sql="SELECT 1")
    # bob 检索不到 alice 的 episode
    assert await store.search(bob, "sales by month", limit=5) == []


async def test_purge(store):
    scope = MemoryScope(datasource="demo", user_id="alice")
    await store.record(scope, question="old", sql="SELECT 1")
    # retention=None → 不删
    assert await store.purge(None) == 0
    assert await store.count(scope) == 1
    # 巨大 retention 也不会误删(时间未超)
    assert await store.purge(99999) == 0
    assert await store.count(scope) == 1
