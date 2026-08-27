"""pg_hybrid KB retrieval backend — query-time retrieval from the unified store.

This is the default backend (replacing ``rag``) for any datasource that has a
hybrid retrieval store (``retrieval_dsn`` or a postgres business DB + an
embedding model). It reuses the unified ``HybridStore`` (PostgreSQL FTS +
pgvector ANN + RRF + rerank) for example/lesson recall, then maps the returned
``doc_id`` (== kb_items.item_key) back to the parsed mirror and re-ranks with
the existing deterministic gates (``_rank_examples`` / ``_rank_lessons``).

Terms keep the builtin substring/word-overlap semantics (vectors don't help
term matching), so ``search_terms`` delegates straight to the KbService.

Graceful degradation: if the store has no docs for the datasource (never
indexed), example/lesson search falls back to the builtin path so retrieval
never silently goes empty.
"""

from __future__ import annotations

import json
from typing import Any

from trove.core.logging import get_logger

logger = get_logger(__name__)

_RECALL_MULTIPLIER = 6
_RECALL_MIN = 8


class PgHybridKbBackend:
    name = "pg_hybrid"

    def __init__(self, kb: Any, store: Any, embedder: Any | None = None) -> None:
        self._kb = kb
        self._store = store
        self._embedder = embedder

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
            return await self._kb._search_examples(
                question, datasource, limit,
                tables=tables, all_tables=all_tables, per_table=per_table)
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
            return await self._kb._search_lessons(
                question, datasource, limit,
                tables=tables, all_tables=all_tables)
        return await self._kb._rank_lessons(
            question, datasource, items, limit,
            tables=tables, all_tables=all_tables, sim_scores=sims)

    async def search_schema_docs(
        self, query: str, datasource: str, limit: int = 5,
    ) -> list[Any]:
        """回灌已索引的物理 schema 元数据(schema_doc):表/列描述 + 枚举值。

        仅在统一 PG 检索库可用时有数据;其他后端(或库空)返回空,调用方据此
        不注入,维持语义优先边界。
        """
        if self._store is None:
            return []
        hits = await self._store.recall(
            query, k=max(limit * 2, 8), rerank_k=limit * 4, datasource=datasource)
        return [h for h in hits if h.kind == "schema_doc"][:limit]

    # ── recall: unified store → kb_items payloads ────────

    async def _recall(
        self, kinds: tuple[str, ...], question: str, datasource: str, recall_limit: int,
    ) -> tuple[list[tuple[int, dict]], dict[int, float]]:
        """Recall from the hybrid store, map doc_id → kb_items, filter by kind."""
        try:
            hits = await self._store.recall(
                question, k=recall_limit, rerank_k=recall_limit * 2,
                datasource=datasource)
        except Exception as e:
            logger.warning("pg_hybrid recall failed for %s: %s", datasource, e)
            return [], {}
        if not hits:
            return [], {}
        keys = [h.doc_id for h in hits]
        ph = ",".join("?" * len(keys))
        rows = await self._kb._rows(
            "SELECT id, item_key, kind, payload FROM kb_items "
            f"WHERE datasource = ? AND item_key IN ({ph})",
            (datasource, *keys),
        )
        by_key = {
            r["item_key"]: (r["id"], r["kind"], json.loads(r["payload"]))
            for r in rows
        }
        items: list[tuple[int, dict]] = []
        sims: dict[int, float] = {}
        for h in hits:
            rec = by_key.get(h.doc_id)
            if rec is None or rec[1] not in kinds:
                continue
            items.append((rec[0], rec[2]))
            sims[rec[0]] = h.score
        return items, sims
