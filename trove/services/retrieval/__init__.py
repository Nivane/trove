"""Unified hybrid retrieval (PostgreSQL FTS + pgvector ANN + RRF + rerank)."""

from trove.services.retrieval.store import (
    HybridStore,
    RetrievalDoc,
    RetrievalHit,
    Reranker,
    rrf_fuse,
)
from trove.services.retrieval.sqlite_store import SqliteHybridStore
from trove.services.retrieval.pg_store import PgHybridStore
from trove.services.retrieval.rerank import (
    DeterministicReranker,
    CrossEncoderReranker,
)

__all__ = [
    "HybridStore",
    "RetrievalDoc",
    "RetrievalHit",
    "Reranker",
    "rrf_fuse",
    "SqliteHybridStore",
    "PgHybridStore",
    "DeterministicReranker",
    "CrossEncoderReranker",
]
