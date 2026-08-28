"""Coarse→fine rerankers (精排 stage after RRF fusion).

The recall stage already fused FTS + ANN via RRF; the reranker reorders the
fused candidates by a finer relevance signal. Implementations:

- ``DeterministicReranker``: zero-LLM, hashed n-gram coverage (reuses the
  existing deterministic rerank signal in ``kb/embeddings.py``). Always safe to
  use as a fallback.
- ``CrossEncoderReranker``: a real cross-encoder pass. If a ``endpoint`` is
  configured (a TEI/Jina/Cohere-style ``/rerank`` HTTP API), it calls it
  directly; otherwise it approximates the cross-encoder with the configured
  embedder by scoring ``cosine(embed(query), embed(doc))``. (litellm has no
  native rerank API, so a local TEI cross-encoder exposed as an
  OpenAI-compatible endpoint is the intended production path.)
- ``BgeReranker``: a local cross-encoder via ``FlagEmbedding``'s
  ``FlagReranker`` (e.g. ``BAAI/bge-reranker-v2-m3``). The industry-standard
  "real cross-encoder" 精排, offline and credential-free (needs
  ``uv sync --extra bge``).
"""

from __future__ import annotations

import math
from typing import Any

import aiohttp

from trove.core.logging import get_logger
from trove.services.kb import embeddings as _emb
from trove.services.retrieval.store import RetrievalHit

logger = get_logger(__name__)


def _deterministic_score(query: str, content: str) -> float:
    try:
        return float(_emb.coverage_score(query, content))
    except Exception:
        return 0.0


class DeterministicReranker:
    """Zero-LLM reranker: hashed n-gram coverage of query over document."""

    async def rerank(
        self, query: str, candidates: list[RetrievalHit], k: int,
    ) -> list[RetrievalHit]:
        for c in candidates:
            c.score = _deterministic_score(query, c.content)
        return sorted(candidates, key=lambda c: c.score, reverse=True)[:k]


class BgeReranker:
    """Local cross-encoder reranker (FlagEmbedding FlagReranker).

    The "real cross-encoder" 精排 default for production: scores each
    (query, doc) pair jointly. Lazily imports FlagEmbedding so the module
    imports without the optional dependency.
    """

    def __init__(self, model: str = "BAAI/bge-reranker-v2-m3") -> None:
        self._model = model
        self._backend: Any = None

    @classmethod
    def available(cls) -> bool:
        """FlagEmbedding 是否可导入(本地 cross-encoder 可用性探测)。"""
        try:
            import FlagEmbedding  # noqa: F401
            return True
        except ImportError:
            return False

    def _load(self) -> Any:
        if self._backend is None:
            try:
                from FlagEmbedding import FlagReranker
            except ImportError as e:  # pragma: no cover - 依赖提示
                raise RuntimeError(
                    "BgeReranker requires 'FlagEmbedding' (uv sync --extra bge)") from e
            self._backend = FlagReranker(self._model)
        return self._backend

    async def rerank(
        self, query: str, candidates: list[RetrievalHit], k: int,
    ) -> list[RetrievalHit]:
        if not candidates:
            return []
        pairs = [[query, c.content] for c in candidates]
        scores = self._load().compute_score(pairs, normalize=True)
        if not isinstance(scores, list):
            scores = [scores]
        for c, s in zip(candidates, scores):
            c.score = float(s)
        return sorted(candidates, key=lambda c: c.score, reverse=True)[:k]


class CrossEncoderReranker:
    """Cross-encoder reranker.

    Args:
        embedder: used for the embedding-cosine approximation when no
            ``endpoint`` is available.
        endpoint: optional HTTP ``/rerank`` URL (TEI/Jina style). When set,
            ``rerank`` posts ``{"query": ..., "texts": [...]}`` and expects a
            JSON list of scores (or ``{"results":[{"index","score"}]}``).
        model: model name passed to the endpoint.
    """

    def __init__(
        self, embedder: Any | None = None, endpoint: str = "", model: str = "",
    ) -> None:
        self._embedder = embedder
        self._endpoint = endpoint.strip()
        self._model = model

    async def rerank(
        self, query: str, candidates: list[RetrievalHit], k: int,
    ) -> list[RetrievalHit]:
        if not candidates:
            return []
        if self._endpoint:
            try:
                scores = await self._http_rerank(query, [c.content for c in candidates])
                for c, s in zip(candidates, scores):
                    c.score = float(s)
                return sorted(candidates, key=lambda c: c.score, reverse=True)[:k]
            except Exception as e:
                logger.warning("cross-encoder endpoint failed, fallback: %s", e)
        if self._embedder is None:
            return candidates[:k]
        q_vec = (await self._embedder.embed([query]))[0]
        doc_vecs = await self._embedder.embed([c.content for c in candidates])
        for c, dv in zip(candidates, doc_vecs):
            c.score = _emb.cosine(q_vec, dv)
        return sorted(candidates, key=lambda c: c.score, reverse=True)[:k]

    async def _http_rerank(self, query: str, texts: list[str]) -> list[float]:
        payload = {"query": query, "texts": texts}
        if self._model:
            payload["model"] = self._model
        async with aiohttp.ClientSession() as session:
            async with session.post(self._endpoint, json=payload, timeout=30) as resp:
                data = await resp.json()
        if isinstance(data, list):
            return [float(x) for x in data]
        results = data.get("results", data.get("scores", []))
        return [float(r["score"]) for r in results]
