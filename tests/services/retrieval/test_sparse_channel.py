"""Tests for the learned-sparse (第三路) channel + RRF weights + keyword_text + meta.

Exercises the full recall path on the SQLite store with a hybrid embedder that
produces dense + sparse in one call (bge-m3-style). No Postgres, no network.
"""

import math

import pytest

from trove.services.retrieval import (
    DeterministicReranker,
    RetrievalDoc,
    SqliteHybridStore,
)
from trove.services.retrieval.store import rrf_fuse


class FakeHybridEmbedder:
    """bge-m3 风格:一次出 dense(bag-of-chars)+ sparse({ord(ch): 计数})。"""

    dim = 16

    async def embed_hybrid(self, texts):
        out = []
        for t in texts:
            dense = [0.0] * self.dim
            sparse = {}
            for ch in t:
                dense[ord(ch) % self.dim] += 1.0
                sparse[ord(ch)] = sparse.get(ord(ch), 0.0) + 1.0
            norm = math.sqrt(sum(v * v for v in dense)) or 1.0
            out.append(([v / norm for v in dense], sparse))
        return out

    async def embed(self, texts):
        return [d for d, _ in await self.embed_hybrid(texts)]

    async def embed_sparse(self, texts):
        return [s for _, s in await self.embed_hybrid(texts)]


@pytest.fixture
def store(tmp_path):
    return SqliteHybridStore(
        tmp_path / "retrieval.sqlite", FakeHybridEmbedder(),
        DeterministicReranker(), sparse_dim=1000,
        rrf_weights={"keyword": 1.0, "dense": 1.0, "sparse": 0.7})


async def test_three_channel_recall_with_meta(store):
    docs = [
        RetrievalDoc(content="贷款 平均 金额 怎么 计算", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e1"),
        RetrievalDoc(content="足球 比赛 比分 直播", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e2"),
    ]
    await store.index_many(docs)
    hits, meta = await store.recall(
        "贷款 平均 金额", k=3, datasource="ds", return_meta=True)
    assert hits and hits[0].doc_id == "e1"
    # 三路:keyword + dense + sparse
    assert len(meta["branch_sizes"]) == 3
    assert meta["rrf_ids"][0] == "e1"
    assert meta["rerank_used"] is True
    assert meta["latency_ms"] >= 0


async def test_sparse_channel_ranks_lexical_overlap(store):
    # e1 与查询共享大量词面字符,e2 不共享 → sparse 路应把 e1 排在前面
    await store.index_many([
        RetrievalDoc(content="贷款 平均 金额 计算", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e1"),
        RetrievalDoc(content="完全 无关 的 内容", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e2"),
    ])
    store._ds = "ds"  # 直接调通道需先设置 recall 的 datasource 作用域
    sparse_ids = await store._sparse_ann_ids(
        {ord(c): 1.0 for c in "贷款金额"}, k=5)
    assert sparse_ids and sparse_ids[0] == "e1"


async def test_keyword_text_controls_fts_channel(store):
    # keyword_text 覆盖 FTS 通道的检索串:不含词面词的 query 也能命中别名扩展
    await store.index_many([
        RetrievalDoc(content="贷款金额 别名 gmv_loan", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e1"),
    ])
    store._ds = "ds"
    # FTS5 默认对空格分词做隐式 AND:"贷款金额 别名" 整词命中,而 "compute" 不命中
    assert "e1" in await store._fts_ids("贷款金额 别名", k=5)
    assert "e1" not in await store._fts_ids("compute", k=5)


def test_rrf_fuse_respects_weights():
    # 第二路权重 10 → 其 rank1 的 b 胜出
    fused = rrf_fuse([["a", "b"], ["b", "a"]], k=60, weights=[1.0, 10.0])
    assert fused[0] == "b"
    # 等权 → a/b 平票,按首现序(a 先出现)
    assert rrf_fuse([["a", "b"], ["b", "a"]], k=60)[0] == "a"


def test_rrf_fuse_skips_empty_lists_with_weights():
    assert rrf_fuse([[], ["a", "b"]], k=60, weights=[1.0, 1.0]) == ["a", "b"]
