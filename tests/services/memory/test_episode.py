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


# ── ⑤ hybrid 检索(向量通道,fake embedder,零网络) ──────────────

class FakeEmbedder:
    """dict 查表 + 确定性散列兜底:同文本恒同向量,不同文本大概率不同。"""

    def __init__(self, vectors: dict | None = None, calls: list | None = None):
        self._vectors = vectors or {}
        self.calls = calls if calls is not None else []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.extend(texts)
        return [self._vectors.get(t, _hash_vec(t)) for t in texts]


def _hash_vec(text: str, dim: int = 8) -> list[float]:
    out = [0.0] * dim
    for i, ch in enumerate(text):
        out[i % dim] += ord(ch) * (i + 1)
    norm = sum(v * v for v in out) ** 0.5 or 1.0
    return [round(v / norm, 6) for v in out]


async def test_hybrid_cosine_rescues_paraphrase(tmp_path):
    """词面 < 0.5 但语义 ≥ 0.55 → OR 门召回(⑤ 放宽门的核心场景)。"""
    vectors = {
        "loan amount by area": [1.0, 0.0, 0.0],      # 查询措辞
        "credit sum per district": [1.0, 0.0, 0.0],  # 同语义不同措辞
    }
    store = EpisodeStore(
        tmp_path / "episodes.sqlite", embedder_factory=lambda ds: FakeEmbedder(vectors),
    )
    scope = MemoryScope(datasource="demo", user_id="alice")
    await store.record(scope, question="credit sum per district",
                       sql="SELECT 1", verdict="OK")
    await store.record(scope, question="weather forecast in paris",
                       sql="SELECT 2", verdict="OK")
    hits = await store.search(scope, "loan amount by area", limit=5)
    # 措辞不同:纯词面路径下两条都不该回;向量通道让 paraphrase 那条过关
    assert len(hits) == 1
    assert hits[0].content["question"] == "credit sum per district"
    assert hits[0].score > 0.5  # 0.6·cos + 0.4·lex + 最近度


async def test_hybrid_no_embedder_stays_lexical(tmp_path):
    """无 embedder 工厂 → 纯词面路径(回归安全:旧行为原样)。"""
    store = EpisodeStore(tmp_path / "episodes.sqlite")  # 无工厂
    scope = MemoryScope(datasource="demo", user_id="alice")
    await store.record(scope, question="average loan amount by region",
                       sql="SELECT 1", verdict="OK")
    await store.record(scope, question="unrelated weather",
                       sql="SELECT 2", verdict="OK")
    hits = await store.search(scope, "average loan amount by region", limit=5)
    assert len(hits) == 1
    assert hits[0].content["question"] == "average loan amount by region"


async def test_embed_text_includes_corrections_and_tables(tmp_path):
    """写入与检索的嵌入文本都含问题 + 命中表 + 修正史(⑤ 修正史入检索)。"""
    calls: list[str] = []
    store = EpisodeStore(
        tmp_path / "episodes.sqlite",
        embedder_factory=lambda ds: FakeEmbedder(calls=calls),
    )
    scope = MemoryScope(datasource="demo", user_id="alice")
    await store.record(
        scope, question="loan by region",
        sql="SELECT 1", verdict="OK",
        matched_tables=["loans", "regions"],
        correction_history=["regions -> region"],
    )
    # 写入嵌入:问题 + 命中表 + 修正史同文本
    assert any("loan by region loans regions" in t for t in calls)
    assert any("regions -> region" in t for t in calls)
    calls.clear()
    await store.search(scope, "loan by region", limit=5)
    # 查询侧只嵌问题(候选侧文本在库里)
    assert any(t == "loan by region" for t in calls)


async def test_search_window_500(tmp_path):
    """候选窗口 200 → 500:第 500 条(旧窗口会丢)仍可被召回。"""
    store = EpisodeStore(
        tmp_path / "episodes.sqlite", embedder_factory=lambda ds: None,
    )
    scope = MemoryScope(datasource="demo", user_id="alice")
    for i in range(499):
        await store.record(scope, question=f"filler {i}", sql="SELECT 1", verdict="OK")
    await store.record(scope, question="the needle query", sql="SELECT 2", verdict="OK")
    hits = await store.search(scope, "the needle query", limit=5)
    assert any(h.content["question"] == "the needle query" for h in hits)


async def test_embed_failure_never_blocks_record(tmp_path):
    """嵌入抛错 → 记录照写(纯词面路径),检索不炸。"""
    class BrokenEmbedder:
        async def embed(self, texts):
            raise RuntimeError("embed backend down")

    store = EpisodeStore(
        tmp_path / "episodes.sqlite",
        embedder_factory=lambda ds: BrokenEmbedder(),
    )
    scope = MemoryScope(datasource="demo", user_id="alice")
    await store.record(scope, question="average loan by region",
                       sql="SELECT 1", verdict="OK")
    assert await store.count(scope) == 1
    hits = await store.search(scope, "average loan by region", limit=5)
    assert len(hits) == 1
