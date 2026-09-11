"""Tests for the pg_hybrid KB retrieval backend (query-time path).

Uses a fake store + fake KbService so no Postgres is needed. Validates the
recall → kb_items payload mapping, kind filtering, and the builtin fallback
when the store has no docs.
"""

import json

import pytest

from trove.core.types import DatasourceConfig
from trove.services.kb.backends.pg_hybrid import PgHybridKbBackend
from trove.services.kb.backends.registry import _effective_backend
from trove.services.retrieval.store import RetrievalHit


class FakeStore:
    def __init__(self, hits, n_docs=None):
        self._hits = hits
        # 默认对齐返回的 hit 数;要模拟大库就显式传 n_docs。
        self._n_docs = len(hits) if n_docs is None else n_docs
        self.calls = []

    async def count(self, datasource):
        return self._n_docs

    async def recall(self, query, k=20, rerank_k=40, datasource="", keyword_text=None,
                     return_meta=False):
        self.calls.append({"query": query, "k": k, "rerank_k": rerank_k,
                           "keyword_text": keyword_text})
        return self._hits


class FakeKb:
    def __init__(self, rows, fallback=None):
        self._rows_data = rows
        self._fallback = fallback
        self.calls = []

    async def _rows(self, sql, params):
        return self._rows_data

    async def _search_terms(self, *a, **k):
        self.calls.append("terms")
        return getattr(self, "_terms", [])

    async def _search_examples(self, *a, **k):
        self.calls.append("examples")
        return self._fallback

    async def _search_lessons(self, *a, **k):
        self.calls.append("lessons")
        return self._fallback


def _payload(question, sql, tags):
    return json.dumps({"question": question, "sql": sql, "tags": tags})


async def test_recall_maps_doc_id_to_payload_and_filters_kind():
    rows = [
        {"id": 7, "item_key": "ex1", "kind": "example",
         "payload": _payload("贷款平均金额", "SELECT ...", ["loan"])},
        {"id": 8, "item_key": "ex2", "kind": "lesson",
         "payload": _payload("pattern", "sql", [])},
    ]
    kb = FakeKb(rows)
    kb._rows_data = rows
    store = FakeStore([
        RetrievalHit(doc_id="ex1", content="贷款平均金额", score=0.9, kind="kb"),
        RetrievalHit(doc_id="ex2", content="lesson", score=0.8, kind="kb"),
    ])
    backend = PgHybridKbBackend(kb, store)
    items, sims = await backend._recall(("example", "template"), "q", "ds", 8)
    # only ex1 (kind=example) passes the kind filter
    assert [i[0] for i in items] == [7]
    assert sims[7] == pytest.approx(0.9)


async def test_empty_store_falls_back_to_builtin():
    kb = FakeKb([], fallback="FALLBACK")
    kb._rows_data = []
    store = FakeStore([])
    backend = PgHybridKbBackend(kb, store)
    res = await backend.search_examples("q", "ds", limit=3)
    assert res == "FALLBACK"
    assert "examples" in kb.calls


async def test_keyword_expands_with_term_aliases():
    from trove.services.kb.service import TermHit

    kb = FakeKb([])
    kb._terms = [
        TermHit(term="loan_amount", aliases=["贷款金额", "gmv_loan"]),
    ]
    store = FakeStore([RetrievalHit(doc_id="ex1", content="x", score=0.9, kind="kb")])
    backend = PgHybridKbBackend(kb, store)
    await backend._recall(("example",), "怎么算 贷款金额", "ds", 8)
    assert store.calls[-1]["keyword_text"] is not None
    assert "贷款金额" in store.calls[-1]["keyword_text"]
    assert "gmv_loan" in store.calls[-1]["keyword_text"]


async def test_keyword_expansion_absent_when_no_terms():
    kb = FakeKb([])
    kb._terms = []
    store = FakeStore([RetrievalHit(doc_id="ex1", content="x", score=0.9, kind="kb")])
    backend = PgHybridKbBackend(kb, store)
    await backend._recall(("example",), "how to compute", "ds", 8)
    assert store.calls[-1]["keyword_text"] is None


async def test_search_schema_docs_returns_only_schema_doc_hits():
    kb = FakeKb([])
    store = FakeStore([
        RetrievalHit(doc_id="schema:card", content="CARD 表: 客户卡", score=0.9, kind="schema_doc"),
        RetrievalHit(doc_id="ex1", content="example", score=0.8, kind="kb"),
    ])
    backend = PgHybridKbBackend(kb, store)
    docs = await backend.search_schema_docs("卡 平均", "ds", limit=5)
    assert [d.doc_id for d in docs] == ["schema:card"]
    assert docs[0].content == "CARD 表: 客户卡"
    # schema_doc 每表一条、在库里占比极小,若按 limit*2 裁候选会被数量占优的
    # kb 文档整批挤出 → 窗口必须按全库规模取。
    assert store.calls[-1]["k"] == 2


@pytest.mark.parametrize("total,limit,expected", [
    (0, 5, (0, 0)),                        # 空库:不去 recall,交给 builtin 兜底
    (1, 5, (1, 1)),                        # 一条也是全量
    (262, 3, (262, 262)),                  # 小库:全量候选,不裁剪
    (2000, 3, (2000, 2000)),               # 恰在上限:仍走全量
    (2001, 3, (18, 36)),                   # 刚过上限:按 limit*6 裁
    (500000, 10, (60, 120)),               # 大库:limit 主导
    (500000, 1, (8, 16)),                  # limit 很小时托底 _RECALL_MIN
])
async def test_recall_window_size_adaptive(total, limit, expected):
    """小库不裁(全量候选),大库按 limit 倍数裁;窗口按**全库**计数。

    小库裁剪只会白丢候选:几百行的全扫是免费的,而 ANN 的价值是避免扫描。
    """
    backend = PgHybridKbBackend(FakeKb([]), FakeStore([], n_docs=total))
    assert await backend._recall_window(limit, "ds") == expected


async def test_empty_corpus_skips_store_recall():
    """空库时不该拿着 k=0 去问 store(会白跑一次 embed + 查询)。"""
    store = FakeStore([], n_docs=0)
    backend = PgHybridKbBackend(FakeKb([]), store)
    assert await backend._recall(("example",), "q", "ds", 5) == ([], {})
    assert store.calls == []


def test_effective_backend_upgrades_to_pg_hybrid_when_viable():
    cfg = DatasourceConfig(
        name="d", type="postgres", retrieval_dsn="postgresql://x",
        embedding_model="text-embedding-3-small")
    assert _effective_backend(cfg) == "pg_hybrid"


def test_effective_backend_upgrades_with_bge_m3_without_model():
    cfg = DatasourceConfig(
        name="d", type="postgres", retrieval_dsn="postgresql://x",
        embedder_backend="bge-m3")
    assert _effective_backend(cfg) == "pg_hybrid"


def test_effective_backend_keeps_explicit_rag():
    cfg = DatasourceConfig(name="d", type="postgres", retrieval_backend="rag")
    assert _effective_backend(cfg) == "rag"


def test_effective_backend_no_embedder_stays_builtin():
    cfg = DatasourceConfig(name="d", type="postgres", retrieval_dsn="postgresql://x")
    assert _effective_backend(cfg) == "builtin"
