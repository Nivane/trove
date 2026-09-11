"""Build a ``HybridStore`` from datasource config + LLM gateway.

Centralizes the dsn/embedder/reranker/channel resolution so the KB retrieval
backend registry, the ``Indexer`` (CLI / admin) and the eval scripts use the
exact same store. Channel config (RRF k / RRF weights) and the query-log
recorder are applied here too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.services.kb.backends.dense import build_embedder
from trove.services.retrieval import (
    BgeReranker,
    CrossEncoderReranker,
    DeterministicReranker,
    PgHybridStore,
    SqliteHybridStore,
)
from trove.services.retrieval.query_log import QueryLogRecorder

logger = get_logger(__name__)

_DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


def _reranker_for(cfg: Any, embedder: Any) -> Any:
    """精排后端选择(``rerank_backend``)。

    **默认(""/"auto"/"none")= 不精排。** 默认档曾经是确定性 n-gram coverage,
    而下游 ``_rank_examples`` 的 ``_sim`` 用的是同一个函数、同源文本 ——
    ``_fuse_extra_sim`` 融合两个同源的量等于原值,那一级纯属重复计算,还额外
    花掉一次全候选打分。去掉后归一化的 RRF 分直接进下游门,信息不比原来少。

    要精排必须显式配:"deterministic"(n-gram coverage) | "bge"(本地
    FlagReranker) | "http"(rerank_endpoint 的 Cohere/TEI 兼容 API) |
    "cross-encoder"(有端点走端点,否则 embedder cosine 近似)。
    """
    backend = str(getattr(cfg, "rerank_backend", "") or "").strip().lower()
    model = str(getattr(cfg, "rerank_model", "") or "").strip()
    endpoint = str(getattr(cfg, "rerank_endpoint", "") or "").strip()
    if backend in ("", "auto", "none"):
        return None
    if backend == "deterministic":
        return DeterministicReranker()
    if backend == "http":
        return CrossEncoderReranker(embedder, endpoint=endpoint, model=model)
    if backend in ("bge", "bge-reranker"):
        return BgeReranker(model=model or _DEFAULT_RERANK_MODEL)
    if backend == "cross-encoder":
        if endpoint:
            return CrossEncoderReranker(embedder, endpoint=endpoint, model=model)
        if model:
            if BgeReranker.available():
                return BgeReranker(model)
            if embedder is not None:
                return CrossEncoderReranker(embedder, model=model)
        return DeterministicReranker()
    logger.warning("unknown rerank_backend %r; rerank disabled", backend)
    return None


def channel_cfg(cfg: Any) -> tuple[int, dict[str, float]]:
    """(rrf_k, rrf_weights) 通道配置(registry / eval 共用)。"""
    return (
        int(getattr(cfg, "rrf_k", 60) or 60),
        dict(getattr(cfg, "rrf_weights", None) or {}),
    )


def build_store(cfg: Any, gateway: Any, home: str | Path) -> Any:
    """Return a HybridStore for one datasource config.

    Priority: explicit ``retrieval_dsn`` → derived postgres business DSN →
    SQLite hybrid fallback. Embedder via ``embedder_backend``/``embedding_model``;
    reranker via ``rerank_backend`` (default off); RRF weights/k from config;
    hit log recorder under ``home/retrieval/query_log.sqlite``.
    """
    embedder = build_embedder(cfg, gateway)
    reranker = _reranker_for(cfg, embedder)
    dims = int(getattr(cfg, "embedding_dims", 1536) or 1536)
    rrf_k, rrf_weights = channel_cfg(cfg)
    recorder = QueryLogRecorder.for_home(home)
    dsn = str(getattr(cfg, "retrieval_dsn", "") or "").strip()
    if not dsn and getattr(cfg, "type", "") == "postgres":
        from trove.services.kb.backends.dense import _vector_dsn
        dsn = _vector_dsn(cfg)
    if dsn:
        return PgHybridStore(
            dsn, embedder=embedder, reranker=reranker, dims=dims,
            rrf_k=rrf_k, rrf_weights=rrf_weights,
            recorder=recorder,
        )
    return SqliteHybridStore.for_home(
        home, embedder, reranker,
        rrf_k=rrf_k, rrf_weights=rrf_weights, recorder=recorder)
