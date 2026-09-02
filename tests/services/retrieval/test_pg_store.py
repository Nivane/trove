"""PostgreSQL hybrid store integration test (env-gated).

Run with a real Postgres that has the ``vector`` extension available:

    PG_TEST_URL=postgresql://user:pass@localhost:5432/trove pytest \
        -m integration tests/services/retrieval/test_pg_store.py

Skipped automatically when PG_TEST_URL is unset.
"""

import os

import pytest

from trove.services.retrieval import (
    DeterministicReranker,
    PgHybridStore,
    RetrievalDoc,
)

pytestmark = pytest.mark.integration
PG_URL = os.environ.get("PG_TEST_URL")


@pytest.fixture
async def store():
    if not PG_URL:
        pytest.skip("PG_TEST_URL not set")
    s = PgHybridStore(PG_URL, None, DeterministicReranker(), dims=16)
    await s.clear("ds")
    yield s
    await s.clear("ds")


async def test_pg_recall_with_ann_and_fts(store):
    # embedder is None here → we supply vectors directly so recall can embed
    # the query; use a trivial embedder for the test.
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

    store._embedder = FakeEmbedder()
    docs = [
        RetrievalDoc(content="贷款 平均 金额 怎么 计算", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e1"),
        RetrievalDoc(content="足球 比赛 比分 直播", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e2"),
    ]
    await store.index_many(docs)
    hits = await store.recall("贷款 平均 金额", k=3, datasource="ds")
    assert hits and hits[0].doc_id == "ds:a.yml:e1"


async def test_pg_sparse_channel_and_meta():
    """learned-sparse 第三路:embedder 一次出 dense+sparse,recall 融合三路。"""
    if not PG_URL:
        pytest.skip("PG_TEST_URL not set")

    class FakeHybridEmbedder:
        dim = 16

        async def embed_hybrid(self, texts):
            import math

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

    s = PgHybridStore(
        PG_URL, None, None, dims=16, sparse_dim=1000,
        rrf_weights={"keyword": 1.0, "dense": 1.0, "sparse": 0.7})
    s._embedder = FakeHybridEmbedder()
    await s.clear("ds")
    try:
        await s.index_many([
            RetrievalDoc(content="贷款 平均 金额 怎么 计算", datasource="ds",
                         kind="kb", source_file="a.yml", item_key="e1"),
            RetrievalDoc(content="足球 比赛 比分 直播", datasource="ds",
                         kind="kb", source_file="a.yml", item_key="e2"),
        ])
        hits, meta = await s.recall(
            "贷款 平均 金额", k=3, datasource="ds", return_meta=True)
        assert hits and hits[0].doc_id == "ds:a.yml:e1"
        assert len(meta["branch_sizes"]) == 3  # keyword + dense + sparse
    finally:
        await s.clear("ds")
