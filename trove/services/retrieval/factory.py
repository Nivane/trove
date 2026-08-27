"""Build a ``HybridStore`` from datasource config + LLM gateway.

Centralizes the dsn/embedder/reranker resolution so both the KB retrieval
backend registry and the ``Indexer`` (CLI / admin) use the exact same store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trove.services.kb.backends.dense import GatewayEmbedder
from trove.services.retrieval import (
    CrossEncoderReranker,
    DeterministicReranker,
    PgHybridStore,
    SqliteHybridStore,
)


def _embedder_for(cfg: Any, gateway: Any):
    model = str(getattr(cfg, "embedding_model", "") or "")
    return GatewayEmbedder(gateway, model) if model else None


def _reranker_for(cfg: Any, embedder: Any):
    model = str(getattr(cfg, "rerank_model", "") or "")
    if model:
        return CrossEncoderReranker(embedder, model=model)
    return DeterministicReranker()


def build_store(cfg: Any, gateway: Any, home: str | Path) -> Any:
    """Return a HybridStore for one datasource config.

    Priority: explicit ``retrieval_dsn`` → derived postgres business DSN →
    SQLite hybrid fallback. Embedder requires ``embedding_model``; reranker uses
    ``rerank_model`` (else deterministic).
    """
    embedder = _embedder_for(cfg, gateway)
    reranker = _reranker_for(cfg, embedder)
    dims = int(getattr(cfg, "embedding_dims", 1536) or 1536)
    dsn = str(getattr(cfg, "retrieval_dsn", "") or "").strip()
    if not dsn and getattr(cfg, "type", "") == "postgres":
        from trove.services.kb.backends.dense import _vector_dsn
        dsn = _vector_dsn(cfg)
    if dsn:
        return PgHybridStore(dsn, embedder=embedder, reranker=reranker, dims=dims)
    return SqliteHybridStore.for_home(home, embedder, reranker)
