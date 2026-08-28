"""Build a ``HybridStore`` from datasource config + LLM gateway.

Centralizes the dsn/embedder/reranker/channel resolution so the KB retrieval
backend registry, the ``Indexer`` (CLI / admin) and the eval scripts use the
exact same store. Channel config (sparse dims / RRF k / RRF weights) and the
query-log recorder are applied here too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trove.services.kb.backends.dense import build_embedder
from trove.services.retrieval import (
    BgeReranker,
    CrossEncoderReranker,
    DeterministicReranker,
    PgHybridStore,
    SqliteHybridStore,
)
from trove.services.retrieval.query_log import QueryLogRecorder

_DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


def _reranker_for(cfg: Any, embedder: Any) -> Any:
    """精排后端选择(``rerank_backend``)。

    ""/"auto": rerank_endpoint → 本地 bge-reranker → embedder cosine 近似 →
    确定性 n-gram(零成本兜底)。显式 "none"/"deterministic"/"bge"/"http"/
    "cross-encoder" 直接对应。
    """
    backend = str(getattr(cfg, "rerank_backend", "") or "").strip().lower()
    model = str(getattr(cfg, "rerank_model", "") or "").strip()
    endpoint = str(getattr(cfg, "rerank_endpoint", "") or "").strip()
    if backend == "none":
        return None
    if backend == "deterministic":
        return DeterministicReranker()
    if backend == "http":
        return CrossEncoderReranker(embedder, endpoint=endpoint, model=model)
    if backend in ("bge", "bge-reranker"):
        return BgeReranker(model=model or _DEFAULT_RERANK_MODEL)
    if backend in ("cross-encoder", ""):
        if endpoint:
            return CrossEncoderReranker(embedder, endpoint=endpoint, model=model)
        if model:
            if BgeReranker.available():
                return BgeReranker(model)
            if embedder is not None:
                return CrossEncoderReranker(embedder, model=model)
            return DeterministicReranker()
        return DeterministicReranker()
    return DeterministicReranker()


def channel_cfg(cfg: Any) -> tuple[int, int, dict[str, float]]:
    """(sparse_dim, rrf_k, rrf_weights) 通道配置(registry / eval 共用)。"""
    return (
        int(getattr(cfg, "embedding_sparse_dims", 0) or 0),
        int(getattr(cfg, "rrf_k", 60) or 60),
        dict(getattr(cfg, "rrf_weights", None) or {}),
    )


def build_store(cfg: Any, gateway: Any, home: str | Path) -> Any:
    """Return a HybridStore for one datasource config.

    Priority: explicit ``retrieval_dsn`` → derived postgres business DSN →
    SQLite hybrid fallback. Embedder via ``embedder_backend``/``embedding_model``;
    reranker via ``rerank_backend`` (else deterministic). Sparse channel enabled
    by ``embedding_sparse_dims``; RRF weights/k from config; hit log recorder
    under ``home/retrieval/query_log.sqlite``.
    """
    embedder = build_embedder(cfg, gateway)
    reranker = _reranker_for(cfg, embedder)
    dims = int(getattr(cfg, "embedding_dims", 1536) or 1536)
    sparse_dim, rrf_k, rrf_weights = channel_cfg(cfg)
    recorder = QueryLogRecorder.for_home(home)
    dsn = str(getattr(cfg, "retrieval_dsn", "") or "").strip()
    if not dsn and getattr(cfg, "type", "") == "postgres":
        from trove.services.kb.backends.dense import _vector_dsn
        dsn = _vector_dsn(cfg)
    if dsn:
        return PgHybridStore(
            dsn, embedder=embedder, reranker=reranker, dims=dims,
            sparse_dim=sparse_dim, rrf_k=rrf_k, rrf_weights=rrf_weights,
            recorder=recorder,
        )
    return SqliteHybridStore.for_home(
        home, embedder, reranker, sparse_dim=sparse_dim,
        rrf_k=rrf_k, rrf_weights=rrf_weights, recorder=recorder)
