"""Unified hybrid retrieval store — sparse BM25 + dense ANN + learned-sparse ANN + RRF + rerank.

Phase 2 of the hybrid-retrieval upgrade. A single retrieval DB (PostgreSQL,
config ``retrieval_dsn``) holds:

- a ``tsvector``/``pg_bm25`` column (BM25-ish / true BM25) for the keyword recall,
- a ``pgvector`` dense column with an HNSW ANN index for semantic recall,
- an optional ``pgvector`` ``sparsevec`` column (learned sparse, e.g. bge-m3
  lexical) with an HNSW index for the learned-sparse channel — the industry
  "stronger-than-BM25" sparse path.

``recall`` runs the channels in parallel, fuses with Reciprocal Rank Fusion
(RRF, optional per-channel weights + configurable ``k``), then a pluggable
reranker does the coarse→fine (精排) pass. A pluggable ``recorder`` captures
per-query hit meta (branch sizes / RRF order / rerank order) for the feedback
loop (see ``trove.services.retrieval.query_log``).

Non-PostgreSQL datasources / test environments fall back to
``SqliteHybridStore`` (FTS5 + in-Python cosine) so the same interface works
everywhere; the PostgreSQL path is exercised by env-gated integration tests.

Documents are not limited to KB YAML — physical schema metadata is indexed as
``schema_doc`` kind (see ``indexer.py``), so catalog probing results become
retrievable too, without bypassing the semantic-first boundary.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from trove.core.logging import get_logger
from trove.services.kb.backends.dense import Embedder

logger = get_logger(__name__)

RRF_K = 60

_CHANNEL_NAMES = ("keyword", "dense", "sparse")


@dataclass
class RetrievalDoc:
    """A retrievable document.

    ``id`` is a stable key (usually ``f"{kind}:{source_file}:{item_key}"``)
    so re-indexing is idempotent. ``embedding`` is optional — the store
    embeds ``content`` when omitted (and computes the learned-sparse vector
    from ``content`` too when the embedder supports it).
    """

    content: str
    datasource: str
    kind: str = "kb"
    source_file: str = ""
    item_key: str = ""
    embedding: list[float] | None = None


@dataclass
class RetrievalHit:
    doc_id: str
    content: str
    score: float
    kind: str = ""


@runtime_checkable
class Reranker(Protocol):
    """Coarse→fine reranker: reorders fused candidates by relevance to query."""

    async def rerank(
        self, query: str, candidates: list[RetrievalHit], k: int,
    ) -> list[RetrievalHit]:
        ...


def rrf_fuse(
    ranked_lists: list[list[str]], k: int = RRF_K,
    weights: list[float] | None = None,
) -> list[str]:
    """Reciprocal Rank Fusion over multiple ranked id lists.

    Each list is already ordered best→worst. ``weights`` aligns with the lists
    (default all 1.0); score = sum(weight / (k + rank)). Returns ids ordered by
    fused score descending (ties by first seen).
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    scores: dict[str, float] = {}
    for ranked, w in zip(ranked_lists, weights):
        if not ranked:
            continue
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + w / (k + rank)
    return sorted(scores, key=lambda d: scores[d], reverse=True)


class HybridStore(ABC):
    """Hybrid retrieval store interface (sparse keyword + dense + learned-sparse + rerank)."""

    def __init__(
        self,
        embedder: Embedder | None,
        reranker: Reranker | None,
        *,
        sparse_dim: int = 0,
        rrf_k: int = RRF_K,
        rrf_weights: dict[str, float] | None = None,
        recorder: Any | None = None,
    ) -> None:
        self._embedder = embedder
        self._reranker = reranker
        self._sparse_dim = int(sparse_dim or 0)
        self._rrf_k = int(rrf_k or RRF_K)
        self._rrf_weights = rrf_weights or {}
        self._recorder = recorder

    def _channel_weights(self, n: int) -> list[float]:
        """每路 RRF 权重(按 keyword/dense/sparse 顺序对齐通道)。"""
        return [
            float(self._rrf_weights.get(_CHANNEL_NAMES[i], 1.0)) for i in range(n)
        ]

    async def _embed(self, text: str) -> list[float]:
        if self._embedder is None:
            raise RuntimeError("no embedder configured for hybrid store")
        vectors = await self._embedder.embed([text])
        return vectors[0]

    async def _embed_hybrid(self, text: str) -> tuple[list[float], dict[int, float] | None]:
        """稠密 + learned-sparse 一次出;不支持 sparse → (dense, None)。"""
        if self._embedder is None:
            raise RuntimeError("no embedder configured for hybrid store")
        if hasattr(self._embedder, "embed_hybrid"):
            dense, sparse = (await self._embedder.embed_hybrid([text]))[0]
            return dense, sparse if self._sparse_dim else None
        dense = (await self._embedder.embed([text]))[0]
        sparse = None
        if self._sparse_dim and hasattr(self._embedder, "embed_sparse"):
            sparse = (await self._embedder.embed_sparse([text]))[0]
        return dense, sparse

    @abstractmethod
    async def index(self, doc: RetrievalDoc) -> None:
        ...

    @abstractmethod
    async def index_many(self, docs: list[RetrievalDoc]) -> None:
        ...

    @abstractmethod
    async def delete_source(self, datasource: str, source_file: str) -> None:
        ...

    @abstractmethod
    async def clear(self, datasource: str) -> None:
        ...

    @abstractmethod
    async def _fts_ids(self, text: str, k: int) -> list[str]:
        ...

    @abstractmethod
    async def _ann_ids(self, vector: list[float], k: int) -> list[str]:
        ...

    async def _sparse_ann_ids(self, sparse: dict[int, float], k: int) -> list[str]:
        """learned-sparse 近邻 top-k。默认实现 = 空(未启用 sparse 路);
        PgHybridStore / SqliteHybridStore 在有 sparse 列时覆写。"""
        return []

    @abstractmethod
    async def _load(self, doc_ids: list[str]) -> list[RetrievalHit]:
        ...

    async def recall(
        self,
        query: str,
        k: int = 20,
        rerank_k: int = 40,
        datasource: str = "",
        keyword_text: str | None = None,
        return_meta: bool = False,
    ) -> list[RetrievalHit] | tuple[list[RetrievalHit], dict]:
        """Recall: keyword ∪ dense ∪ (learned-sparse) → weighted RRF → rerank → top-k.

        ``keyword_text`` overrides the string used for the keyword channel (e.g.
        KB term-alias expanded) while ``query`` stays the embedding input.
        ``datasource`` scopes the recall to one source. ``return_meta=True``
        returns ``(hits, meta)`` where meta carries branch sizes / RRF order /
        rerank order / latency — the feedback-loop + eval surface.
        """
        self._ds = datasource
        t0 = time.perf_counter()
        kw = (keyword_text or query).strip() or query
        vector, sparse = await self._embed_hybrid(query)
        fts_ids = await self._fts_ids(kw, rerank_k)
        ann_ids = await self._ann_ids(vector, rerank_k)
        channels: list[list[str]] = [fts_ids, ann_ids]
        sparse_ids: list[str] = []
        if sparse is not None and self._sparse_dim:
            sparse_ids = await self._sparse_ann_ids(sparse, rerank_k)
            channels.append(sparse_ids)
        fused = rrf_fuse(
            channels, k=self._rrf_k, weights=self._channel_weights(len(channels)))
        candidates = await self._load(fused)
        rrf_ids = [c.doc_id for c in candidates]
        rerank_used = False
        if self._reranker is not None and candidates:
            try:
                candidates = await self._reranker.rerank(query, candidates, rerank_k)
                rerank_used = True
            except Exception as e:  # rerank failure must not break retrieval
                logger.warning("rerank failed, using RRF order: %s", e)
        top = candidates[:k]
        meta = {
            "branch_sizes": [len(c) for c in channels],
            "rrf_ids": rrf_ids,
            "rerank_ids": [c.doc_id for c in top],
            "rerank_used": rerank_used,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
        }
        if self._recorder is not None:
            try:
                await self._recorder.record(query, datasource, meta)
            except Exception as e:
                logger.warning("query log failed: %s", e)
        if return_meta:
            return top, meta
        return top
