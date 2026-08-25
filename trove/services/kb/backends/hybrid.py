"""Sparse retrieval backend — FTS5 + BM25(零成本、零网络、零依赖).

确定性门(表锚/词重叠)仍是"是否返回"的判定;FTS5 提供倒排索引召回,
BM25 提供 IDF 加权排序,两者在门内融合(``_rank_examples`` 的 sim 换成
coverage × BM25 混合,见 service._fuse_extra_sim)。这是双通道 RAG
(sparse + dense)的稀疏通道。

``kb_fts`` 是 ``kb_items`` 的 FTS5 镜像(contentless + UNINDEXED 元数据
列),与 kb_items 同事务同步(_sync_file/_purge_deleted_files),删除传播
天然成立。检索失败(镜像缺失/未同步)退化为空——不烧生成预算。
"""

from __future__ import annotations

import json
from typing import Any

from trove.core.logging import get_logger
from trove.services.kb.backends.fts import fts_query, normalize_bm

logger = get_logger(__name__)

_RECALL_MULTIPLIER = 6       # 召回上限 = limit × 该值
_RECALL_MIN = 8              # 召回下限(limit=1 时仍保证候选池)

_RECALL_SQL = """
SELECT k.id AS id, k.payload AS payload, bm25(kb_fts) AS bm
FROM kb_fts
JOIN kb_items k ON k.id = kb_fts.rowid
WHERE kb_fts MATCH ? AND kb_fts.datasource = ? AND kb_fts.kind IN ({ph})
ORDER BY bm LIMIT ?
"""


class HybridBackend:
    """FTS5 + BM25 稀疏检索后端(hybrid 模式,确定性门内融合)。"""

    name = "hybrid"

    def __init__(self, kb: Any):
        self._kb = kb

    async def search_terms(
        self,
        question: str,
        datasource: str,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
    ) -> list[Any]:
        """term 检索是子串/词重叠语义,FTS5 无法更好——委托 builtin。"""
        return await self._kb._search_terms(question, datasource, tables, all_tables)

    async def search_examples(
        self,
        question: str,
        datasource: str,
        limit: int = 3,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
        per_table: bool = False,
    ) -> list[Any]:
        items, bm = await self._recall(
            ("example", "template"), question, datasource,
            max(limit * _RECALL_MULTIPLIER, _RECALL_MIN),
        )
        if not items:
            return []
        return await self._kb._rank_examples(
            question, datasource, items, limit,
            tables=tables, all_tables=all_tables, per_table=per_table,
            sim_scores=bm,
        )

    async def search_lessons(
        self,
        question: str,
        datasource: str,
        limit: int = 3,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
    ) -> list[dict]:
        items, bm = await self._recall(
            ("lesson",), question, datasource,
            max(limit * _RECALL_MULTIPLIER, _RECALL_MIN),
        )
        if not items:
            return []
        return await self._kb._rank_lessons(
            question, datasource, items, limit,
            tables=tables, all_tables=all_tables, sim_scores=bm,
        )

    async def _recall(
        self, kinds: tuple[str, ...], question: str, datasource: str,
        recall_limit: int,
    ) -> tuple[list[tuple[int, dict]], dict[int, float]]:
        """FTS5 BM25 召回候选:返回 ([(rowid, payload)], {rowid: bm 归一化分})。

        镜像缺失/查询失败 → 空(降级,不抛)。确定性门过滤在调用方。
        """
        q = fts_query(question)
        if not q:
            return [], {}
        ph = ",".join("?" * len(kinds))
        try:
            rows = await self._kb._rows(
                _RECALL_SQL.format(ph=ph),
                (q, datasource, *kinds, recall_limit),
            )
        except Exception:
            logger.warning(
                "FTS5 recall failed for %s/%s (degrading to empty): ",
                datasource, question[:40], exc_info=True,
            )
            return [], {}
        if not rows:
            return [], {}
        items = [(r["id"], json.loads(r["payload"])) for r in rows]
        bm = normalize_bm({r["id"]: r["bm"] for r in rows})
        return items, bm
