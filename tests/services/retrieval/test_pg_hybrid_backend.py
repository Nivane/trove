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
    def __init__(self, hits):
        self._hits = hits

    async def recall(self, query, k=20, rerank_k=40, datasource=""):
        return self._hits


class FakeKb:
    def __init__(self, rows, fallback=None):
        self._rows_data = rows
        self._fallback = fallback
        self.calls = []

    async def _rows(self, sql, params):
        return self._rows_data

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


def test_effective_backend_upgrades_to_pg_hybrid_when_viable():
    cfg = DatasourceConfig(
        name="d", type="postgres", retrieval_dsn="postgresql://x",
        embedding_model="text-embedding-3-small")
    assert _effective_backend(cfg) == "pg_hybrid"


def test_effective_backend_keeps_explicit_rag():
    cfg = DatasourceConfig(name="d", type="postgres", retrieval_backend="rag")
    assert _effective_backend(cfg) == "rag"


def test_effective_backend_no_embedder_stays_builtin():
    cfg = DatasourceConfig(name="d", type="postgres", retrieval_dsn="postgresql://x")
    assert _effective_backend(cfg) == "builtin"
