"""RAG backend — sparse(FTS5/BM25)+ dense(embedding)双通道 RRF 融合.

- 稀疏通道:复用 HybridBackend 的 FTS5 BM25 召回。
- 稠密通道:embedding 查询 kb_vectors 余弦近邻(embedder/vector_store 注入,
  测试用确定性 fake;生产走 LLMGateway + Sqlite/PgVectorStore)。
- RRF 融合:两通道独立召回后 1/(k+rank) 求和归一,作为门内排序信号。
- 硬门保持:确定性分(表锚/词重叠)仍是"是否返回"的判定(见 _rank_examples)。
- 降级链:dense 失败(embedder 不可用/向量库异常)→ 纯稀疏;双通道全空 → 空。
- 索引:YAML 是唯一 truth,``index_file`` 在 kb_items 镜像同步后重建该文件
  向量(与 kb_fts 同哲学);embedding 是慢 LLM 调用,只在文件 mtime 变化时发生。
"""

from __future__ import annotations

import json
from typing import Any

from trove.core.logging import get_logger
from trove.services.kb.backends.fts import fts_item_text
from trove.services.kb.backends.hybrid import HybridBackend

logger = get_logger(__name__)

_RRF_K = 60          # RRF 常数(标准 60)
_RECALL_MULTIPLIER = 6
_RECALL_MIN = 8


def rrf_fuse(
    sparse: dict[int, float], dense: dict[int, float], k: int = _RRF_K,
) -> dict[int, float]:
    """Reciprocal Rank Fusion:两通道相似度(0..1,高=好)融合为 0..1 排序信号。

    双通道独立排序后按 rank 求和 1/(k+rank),归一化到 0..1
    (双通道 rank1 = 1.0)。通道缺失/为空时另一通道独立决定排序。
    """
    scores: dict[int, float] = {}

    def _add(channel: dict[int, float]) -> None:
        for rank, id_ in enumerate(
            sorted(channel, key=channel.get, reverse=True), start=1,
        ):
            scores[id_] = scores.get(id_, 0.0) + 1.0 / (k + rank)

    _add(sparse)
    _add(dense)
    if not scores:
        return {}
    maxv = 2.0 / (k + 1)
    return {id_: s / maxv for id_, s in scores.items()}


class RagBackend:
    """稀疏 + 稠密双通道 RAG 检索后端(rag 模式)。"""

    name = "rag"

    def __init__(self, kb: Any, embedder: Any | None = None, vector_store: Any | None = None):
        self._kb = kb
        self._embedder = embedder
        from trove.services.kb.backends.dense import SqliteVectorStore
        self._vectors = vector_store or SqliteVectorStore(kb)
        self._sparse = HybridBackend(kb)

    # ── RetrievalBackend ─────────────────────────────────

    async def search_terms(
        self, question: str, datasource: str,
        tables: list[str] | None = None, all_tables: list[str] | None = None,
    ) -> list[Any]:
        return await self._kb._search_terms(question, datasource, tables, all_tables)

    async def search_examples(
        self, question: str, datasource: str, limit: int = 3,
        tables: list[str] | None = None, all_tables: list[str] | None = None,
        per_table: bool = False,
    ) -> list[Any]:
        items, sims = await self._recall(
            ("example", "template"), question, datasource,
            max(limit * _RECALL_MULTIPLIER, _RECALL_MIN),
        )
        if not items:
            return []
        return await self._kb._rank_examples(
            question, datasource, items, limit,
            tables=tables, all_tables=all_tables, per_table=per_table,
            sim_scores=sims,
        )

    async def search_lessons(
        self, question: str, datasource: str, limit: int = 3,
        tables: list[str] | None = None, all_tables: list[str] | None = None,
    ) -> list[dict]:
        items, sims = await self._recall(
            ("lesson",), question, datasource,
            max(limit * _RECALL_MULTIPLIER, _RECALL_MIN),
        )
        if not items:
            return []
        return await self._kb._rank_lessons(
            question, datasource, items, limit,
            tables=tables, all_tables=all_tables, sim_scores=sims,
        )

    # ── 双通道召回 ────────────────────────────────────────

    async def _recall(
        self, kinds: tuple[str, ...], question: str, datasource: str, recall_limit: int,
    ) -> tuple[list[tuple[int, dict]], dict[int, float]]:
        """双通道召回 + RRF 融合:返回 ([(id, payload)], {id: 融合相似度})。"""
        sparse_items, sparse_sims = await self._sparse._recall(
            kinds, question, datasource, recall_limit)
        sparse_map = {i: sparse_sims.get(i, 0.0) for i, _ in sparse_items}
        dense_map = await self._dense_recall(kinds, question, datasource, recall_limit)

        fused = rrf_fuse(sparse_map, dense_map)
        if not fused:
            return [], {}
        ids = set(fused)
        items = await self._load_payloads(datasource, ids)
        return items, fused

    async def _dense_recall(
        self, kinds: tuple[str, ...], question: str, datasource: str, recall_limit: int,
    ) -> dict[int, float]:
        """稠密召回:query embedding → 向量近邻;失败/无索引 → 空(稀疏兜底)。"""
        if self._embedder is None:
            return {}
        try:
            qvec = (await self._embedder.embed([question]))[0]
            hits = await self._vectors.query(
                datasource, qvec, kinds, recall_limit)
        except Exception:
            logger.warning(
                "dense recall failed for %s (falling back to sparse): ",
                datasource, exc_info=True,
            )
            return {}
        return {id_: sim for id_, sim in hits}

    async def _load_payloads(
        self, datasource: str, ids: set[int],
    ) -> list[tuple[int, dict]]:
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        rows = await self._kb._rows(
            f"SELECT id, payload FROM kb_items "
            f"WHERE datasource = ? AND id IN ({ph})",
            (datasource, *sorted(ids)),
        )
        return [(r["id"], json.loads(r["payload"])) for r in rows]

    # ── 索引钩子(service 在镜像同步后调用)────────────────

    async def index_file(
        self, datasource: str, source_file: str, entries: list[tuple[str, str, dict]],
    ) -> None:
        """重建一个文件的向量:embedding 批量算完再原子替换(失败不改库)。

        entries = _parse_file 输出 [(kind, item_key, payload)];id 与
        kb_items 按 (datasource, source_file) 同序对齐。
        """
        indexable = [
            (i, kind, payload) for i, (kind, _key, payload) in enumerate(entries)
            if fts_item_text(kind, payload)
        ]
        if not indexable:
            await self._vectors.replace(datasource, source_file, [])
            return
        texts = [fts_item_text(kind, payload) for _, kind, payload in indexable]
        vecs = await self._embedder.embed(texts) if self._embedder is not None else []
        if len(vecs) != len(texts):
            raise ValueError(
                f"embedder returned {len(vecs)} vectors for {len(texts)} texts")
        rows = await self._kb._rows(
            "SELECT id FROM kb_items WHERE datasource = ? AND source_file = ? "
            "ORDER BY id",
            (datasource, source_file),
        )
        # 位置对齐:kb_items id 与 entries 一一对应(同事务按序重插),但
        # indexable 过滤了空文本条目 —— 用 entries 位置 → id 的映射,而不是
        # zip(indexable, rows)(混合 kind 文件会错位:term 无 fts 文本被跳过,
        # rows 却含其 id,向量会挂到错误的 kb_items 行上)。
        id_by_pos = {i: id_ for i, (id_,) in enumerate(rows)}
        items = []
        for (pos, kind, _payload), vec in zip(indexable, vecs):
            items.append((id_by_pos[pos], kind, vec))
        await self._vectors.replace(datasource, source_file, items)

    async def delete_file(self, datasource: str, source_file: str) -> None:
        try:
            await self._vectors.delete_file(datasource, source_file)
        except Exception:
            logger.warning("vector delete failed for %s/%s", datasource, source_file, exc_info=True)

    async def clear(self, datasource: str) -> None:
        try:
            await self._vectors.clear(datasource)
        except Exception:
            logger.warning("vector clear failed for %s", datasource, exc_info=True)
