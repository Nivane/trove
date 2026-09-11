"""Unified hybrid retrieval store — keyword BM25 + dense ANN + RRF (+ optional rerank).

A single retrieval DB (PostgreSQL, config ``retrieval_dsn``) holds:

- a ``tsvector``/``pg_bm25`` column (BM25-ish / true BM25) for the keyword recall,
- a ``pgvector`` dense column with an HNSW ANN index for semantic recall.

两路是**真正独立**的信号(词法 vs 语义),这正是 RRF 的前提。曾经的第三路
learned-sparse(bge-m3 lexical)已移除:它与 dense 同属一个模型的 head,不构成
独立信号;它唯一不可替代的场景是跨语言(词法通道在语言不匹配时归零),而
本项目以「KB 内容语言 = 问题语言」为约束,跨语言不在目标范围内(见 CLAUDE.md)。

``recall`` runs the channels in parallel, fuses with Reciprocal Rank Fusion
(RRF, optional per-channel weights + configurable ``k``), then an optional
pluggable reranker does the coarse→fine (精排) pass — 默认无精排:默认档曾是
确定性 n-gram,与下游 ``_rank_examples`` 的 ``_sim`` 同函数同源,融合等于原值,
纯重复劳动(见 ``retrieval/factory._reranker_for``)。A pluggable ``recorder`` captures
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

_CHANNEL_NAMES = ("keyword", "dense")


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


def rrf_scores(
    ranked_lists: list[list[str]], k: int = RRF_K,
    weights: list[float] | None = None,
) -> dict[str, float]:
    """Reciprocal Rank Fusion 的 **分数**(id → fused score)。

    Each list is already ordered best→worst. ``weights`` aligns with the lists
    (default all 1.0); score = sum(weight / (k + rank)).

    与 :func:`rrf_fuse` 同算法、不同产出:调用方既要顺序(cut 到 top-k),也要
    分数(进 ``_fuse_extra_sim`` 当门内特征)。分开返回是因为 RRF 分**天然压缩**
    ——2 路 k=60 时 raw 落在 [0.022, 0.033],直接当相似度用等于常量偏移。
    要进融合的分数先过 :func:`normalize_scores`。
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    scores: dict[str, float] = {}
    for ranked, w in zip(ranked_lists, weights):
        if not ranked:
            continue
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + w / (k + rank)
    return scores


def normalize_scores(scores: dict[str, float]) -> dict[str, float]:
    """min-max 归一化到 [0,1];空集或全等 → 全 1.0。

    全等(只召回一条、或各路排序完全一致)时 max == min,若归成 0.0 会把
    "最相关"读成"最不相关" —— 退回 1.0 表示"候选中最好的那个"。
    """
    if not scores:
        return {}
    lo = min(scores.values())
    hi = max(scores.values())
    if hi <= lo:
        return {doc_id: 1.0 for doc_id in scores}
    span = hi - lo
    return {doc_id: (v - lo) / span for doc_id, v in scores.items()}


def rrf_fuse(
    ranked_lists: list[list[str]], k: int = RRF_K,
    weights: list[float] | None = None,
) -> list[str]:
    """Reciprocal Rank Fusion over multiple ranked id lists.

    Returns ids ordered by fused score descending (ties by first seen).
    """
    scores = rrf_scores(ranked_lists, k=k, weights=weights)
    return sorted(scores, key=lambda d: scores[d], reverse=True)


class HybridStore(ABC):
    """Hybrid retrieval store interface (keyword + dense + RRF + optional rerank)."""

    def __init__(
        self,
        embedder: Embedder | None,
        reranker: Reranker | None,
        *,
        rrf_k: int = RRF_K,
        rrf_weights: dict[str, float] | None = None,
        recorder: Any | None = None,
    ) -> None:
        self._embedder = embedder
        self._reranker = reranker
        self._rrf_k = int(rrf_k or RRF_K)
        self._rrf_weights = rrf_weights or {}
        self._recorder = recorder

    def _channel_weights(self, n: int) -> list[float]:
        """每路 RRF 权重(按 keyword/dense 顺序对齐通道)。"""
        return [
            float(self._rrf_weights.get(_CHANNEL_NAMES[i], 1.0)) for i in range(n)
        ]

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
    async def delete_kind(self, datasource: str, kind: str) -> None:
        """删除某数据源某一类文档(如全部 ``kb`` 文档)。

        与 ``delete_source`` 的区别是粒度轴:KB 语料的边界是 ``kind`` 而不是
        文件名 —— 条目可能来自**已不存在**的文件,按现存文件名逐个删,恰好
        删不掉它们(rebuild 清理失效的根因)。
        """
        ...

    @abstractmethod
    async def clear(self, datasource: str) -> None:
        ...

    @abstractmethod
    async def count(self, datasource: str) -> int:
        """该数据源在检索库里的文档总数(**跨 kind**)。

        调用方用它决定要不要裁剪召回:ANN/HNSW 存在的意义是避免扫描,
        几百行的全扫是免费的,裁剪只会白丢候选。注意必须按**全库**计数而非
        某一 kind —— 通道的 top-k 是在全库上取的,按单 kind 计数会低估候选
        池,让占比小的 kind(schema_doc、lesson)被大 kind 挤空。
        """

    @abstractmethod
    async def _fts_ids(self, text: str, k: int) -> list[str]:
        ...

    @abstractmethod
    async def _ann_ids(self, vector: list[float], k: int) -> list[str]:
        ...

    @abstractmethod
    async def _load(
        self, doc_ids: list[str], scores: dict[str, float],
    ) -> list[RetrievalHit]:
        """按 ``doc_ids`` 顺序装载命中,``score`` 取 ``scores``(已归一化的
        RRF 分)。装载顺序 = RRF 序,分数与顺序同源,不在这里另造一个。"""
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
        """Recall: keyword ∪ dense → weighted RRF → (可选)rerank → top-k.

        ``keyword_text`` overrides the string used for the keyword channel (e.g.
        KB term-alias expanded) while ``query`` stays the embedding input.
        ``datasource`` scopes the recall to one source. ``return_meta=True``
        returns ``(hits, meta)`` where meta carries branch sizes / RRF order /
        rerank order / latency — the feedback-loop + eval surface.

        命中 ``score`` = **归一化后的 RRF 分**(无精排时)或精排分(有精排时)。
        归一化是必须的:RRF 分压缩在极窄区间,直接当相似度会让下游
        ``_fuse_extra_sim`` 的 0.5 权重退化成常量偏移,压平确定性信号。
        """
        self._ds = datasource
        t0 = time.perf_counter()
        kw = (keyword_text or query).strip() or query
        vector = await self._embed(query)
        fts_ids = await self._fts_ids(kw, rerank_k)
        ann_ids = await self._ann_ids(vector, rerank_k)
        channels: list[list[str]] = [fts_ids, ann_ids]
        fused_scores = rrf_scores(
            channels, k=self._rrf_k, weights=self._channel_weights(len(channels)))
        fused = sorted(fused_scores, key=lambda d: fused_scores[d], reverse=True)
        candidates = await self._load(fused, normalize_scores(fused_scores))
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
