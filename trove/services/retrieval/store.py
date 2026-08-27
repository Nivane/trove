"""Unified hybrid retrieval store — PostgreSQL FTS + pgvector ANN + RRF + rerank.

Phase 1 of the hybrid-retrieval upgrade. A single retrieval DB (PostgreSQL,
config ``retrieval_dsn``) holds both:

- a ``tsvector`` column (native PostgreSQL full-text, BM25-ish) for sparse recall,
- a ``pgvector`` column with an HNSW ANN index for dense recall.

``recall`` runs both channels in parallel, fuses with Reciprocal Rank Fusion
(RRF), then a pluggable reranker does the coarse→fine (精排) pass.

Non-PostgreSQL datasources / test environments fall back to
``SqliteHybridStore`` (FTS5 + in-Python cosine) so the same interface works
everywhere; the PostgreSQL path is exercised by env-gated integration tests.

Documents are not limited to KB YAML — physical schema metadata is indexed as
``schema_doc`` kind (see ``indexer.py``), so catalog probing results become
retrievable too, without bypassing the semantic-first boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from trove.core.logging import get_logger
from trove.services.kb.backends.dense import Embedder

logger = get_logger(__name__)

RRF_K = 60


@dataclass
class RetrievalDoc:
    """A retrievable document.

    ``id`` is a stable key (usually ``f"{kind}:{source_file}:{item_key}"``)
    so re-indexing is idempotent. ``embedding`` is optional — the store
    embeds ``content`` when omitted.
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


def rrf_fuse(ranked_lists: list[list[str]], k: int = RRF_K) -> list[str]:
    """Reciprocal Rank Fusion over multiple ranked id lists.

    Each list is already ordered best→worst; later lists vote with the same
    weight. Returns ids ordered by fused score descending (ties by first seen).
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda d: scores[d], reverse=True)


class HybridStore(ABC):
    """Hybrid retrieval store interface (sparse + dense + rerank)."""

    def __init__(self, embedder: Embedder | None, reranker: Reranker | None) -> None:
        self._embedder = embedder
        self._reranker = reranker

    async def _embed(self, text: str) -> list[float]:
        if self._embedder is None:
            raise RuntimeError("no embedder configured for hybrid store")
        vectors = await self._embedder.embed([text])
        return vectors[0]

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

    @abstractmethod
    async def _load(self, doc_ids: list[str]) -> list[RetrievalHit]:
        ...

    async def recall(
        self, query: str, k: int = 20, rerank_k: int = 40, datasource: str = "",
    ) -> list[RetrievalHit]:
        """Recall: FTS ∪ ANN → RRF fuse → rerank → top-k.

        ``datasource`` scopes the recall to one source (set on the instance so
        the channel helpers can read it without threading it through every call).
        """
        self._ds = datasource
        vector = await self._embed(query)
        fts_ids = await self._fts_ids(query, rerank_k)
        ann_ids = await self._ann_ids(vector, rerank_k)
        fused = rrf_fuse([fts_ids, ann_ids])
        candidates = await self._load(fused)
        if self._reranker is not None and candidates:
            try:
                candidates = await self._reranker.rerank(query, candidates, rerank_k)
            except Exception as e:  # rerank failure must not break retrieval
                logger.warning("rerank failed, using RRF order: %s", e)
        return candidates[:k]
