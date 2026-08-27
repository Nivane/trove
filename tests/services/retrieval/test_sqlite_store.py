"""Tests for the SQLite hybrid store (FTS5 + cosine + RRF + rerank).

Exercises the full recall path without PostgreSQL: index a few docs, run a
query, and assert the RRF-fused + reranked candidates come back ranked by the
configured reranker.
"""

import pytest

from trove.services.retrieval import (
    DeterministicReranker,
    RetrievalDoc,
    SqliteHybridStore,
)
from trove.services.retrieval.rerank import CrossEncoderReranker


class FakeEmbedder:
    dim = 16

    async def embed(self, texts):
        import math

        out = []
        for t in texts:
            vec = [0.0] * self.dim
            for ch in t:
                vec[ord(ch) % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


@pytest.fixture
def store(tmp_path):
    return SqliteHybridStore(
        tmp_path / "retrieval.sqlite", FakeEmbedder(), DeterministicReranker())


async def test_recall_ranks_relevant_first(store):
    docs = [
        RetrievalDoc(content="贷款 平均 金额 怎么 计算", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="ex1"),
        RetrievalDoc(content="完全 无关 的 足球 比赛 比分", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="ex2"),
        RetrievalDoc(content="地区 分布 与 账户 数量 的 关系", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="ex3"),
    ]
    await store.index_many(docs)

    hits = await store.recall("贷款 平均 金额", k=3, datasource="ds")
    assert hits, "expected at least one hit"
    assert hits[0].content.startswith("贷款")


async def test_recall_scoped_by_datasource(store):
    await store.index_many([
        RetrievalDoc(content="贷款 金额", datasource="ds1", kind="kb",
                     source_file="a.yml", item_key="e1"),
        RetrievalDoc(content="贷款 金额", datasource="ds2", kind="kb",
                     source_file="a.yml", item_key="e2"),
    ])
    ds1 = await store.recall("贷款 金额", k=5, datasource="ds1")
    ds2 = await store.recall("贷款 金额", k=5, datasource="ds2")
    assert {h.doc_id for h in ds1} == {"e1"}
    assert {h.doc_id for h in ds2} == {"e2"}


async def test_delete_source(store):
    await store.index_many([
        RetrievalDoc(content="贷款 金额", datasource="ds", kind="kb",
                     source_file="a.yml", item_key="e1"),
    ])
    await store.delete_source("ds", "a.yml")
    hits = await store.recall("贷款 金额", k=5, datasource="ds")
    assert hits == []


async def test_clear(store):
    await store.index_many([
        RetrievalDoc(content="贷款 金额", datasource="ds", kind="kb",
                     source_file="a.yml", item_key="e1"),
    ])
    await store.clear("ds")
    assert await store.recall("贷款 金额", k=5, datasource="ds") == []


async def test_cross_encoder_reranker_used(store):
    # swap to cross-encoder reranker, ensure recall still returns ranked hits
    store._reranker = CrossEncoderReranker(FakeEmbedder())
    await store.index_many([
        RetrievalDoc(content="贷款 平均 金额 是 多少", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e1"),
        RetrievalDoc(content="足球 比赛 直播 时间", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e2"),
    ])
    hits = await store.recall("贷款 平均 金额", k=2, datasource="ds")
    assert hits and hits[0].doc_id == "e1"
