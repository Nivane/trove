"""pg_hybrid KB retrieval backend — query-time retrieval from the unified store.

This is the default backend (replacing ``rag``) for any datasource that has a
hybrid retrieval store (``retrieval_dsn`` or a postgres business DB + an
embedding model). It reuses the unified ``HybridStore`` (PostgreSQL FTS +
pgvector ANN + RRF) for example/lesson recall, then maps the returned
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

#: 候选全量扫描的规模上限(检索库内该数据源的文档数)。低于它就不裁召回:
#: ANN/HNSW 的价值是避免扫描,几百行的全扫是免费的,裁了只会白丢候选。
#: 超过它才按 limit 倍数裁,这时 ANN 才开始真正省钱。
_EXHAUSTIVE_SCAN_MAX = 2000


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
            ("example", "template"), question, datasource, limit)
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
            ("lesson",), question, datasource, limit)
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
        # schema_doc 每表一条,在库里占比极小,而 store.recall 是在**全库**上取
        # top-k 的:按 limit*2 裁候选,它会被数量占优的 kb 文档整批挤出去
        # (实测 262 条库里挑 10 条,8 条 schema_doc 未必进得去)。同样按库规模
        # 自适应 —— 小库全量召回后再按 kind 过滤。
        recall_k, pool_k = await self._recall_window(limit, datasource)
        if not recall_k:
            return []
        hits = await self._store.recall(
            query, k=recall_k, rerank_k=pool_k, datasource=datasource)
        return [h for h in hits if h.kind == "schema_doc"][:limit]

    # ── 索引钩子(KbService 在镜像同步后调用)──────────────
    # 契约与 rag 后端一致:KbService 只认这三个鸭子类型钩子,没有就静默跳过。
    # 本后端是**默认生产后端**,却长期一个都没实现 —— KB 写入对统一检索库
    # 完全不可见。这不是"少召回几条"而是**静默陈旧**:_recall 只在结果为空
    # 时回退 builtin,库一旦被索引过,写入前的那份旧文档就把 fresh 的兜底
    # 压住了;此后新增的条目永远召不回来,而检索看上去一切正常。

    async def index_file(
        self, datasource: str, source_file: str,
        entries: list[tuple[str, str, dict]],
    ) -> None:
        """重建该文件在检索库的文档:先删该文件的旧文档,再写入新的。

        与 ``Indexer.index_kb`` 同构(同文本、同 ``kind``、同 ``item_key``
        即 doc_id)——增量写入与全量索引必须产出逐字段一致的文档,否则同一
        条目会随"最后是谁写的"而不同。
        """
        from trove.services.retrieval.indexer import kb_item_text
        from trove.services.retrieval.store import RetrievalDoc

        docs: list[RetrievalDoc] = []
        for kind, item_key, payload in entries:
            text = kb_item_text(kind, payload)
            if not text:
                continue
            docs.append(RetrievalDoc(
                content=text, datasource=datasource, kind="kb",
                source_file=source_file, item_key=item_key,
            ))
        # 先删后写:条目被改写或从 YAML 移除时,旧文档不能留下
        # (留下的旧 doc_id 映射不回 kb_items,只会白占召回位)。
        await self._store.delete_source(datasource, source_file)
        if docs:
            await self._store.index_many(docs)

    async def delete_file(self, datasource: str, source_file: str) -> None:
        """文件从磁盘移除 → 该文件的文档一并清掉。"""
        await self._store.delete_source(datasource, source_file)

    async def clear(self, datasource: str) -> None:
        """delete_kb → 清空该数据源的全部文档。"""
        await self._store.clear(datasource)

    # ── recall: unified store → kb_items payloads ────────

    async def _expand_keyword(self, question: str, datasource: str) -> str | None:
        """KB term 别名词面扩展:问题命中某 term/别名时,把其全部别名 + 词面
        拼进 keyword 通道的检索串(复用 ``_search_terms`` 的已有语义),让
        FTS/BM25 能命中"别名写法"的文档。稠密通道仍用原始问题 embedding。
        """
        try:
            terms = await self._kb._search_terms(question, datasource, None, None)
        except Exception:
            return None
        extra: list[str] = []
        for t in terms:
            term = getattr(t, "term", "") or ""
            if term:
                extra.append(term)
            for a in getattr(t, "aliases", None) or []:
                if a:
                    extra.append(str(a))
        if not extra:
            return None
        seen: list[str] = []
        for w in extra:
            if w and w not in seen:
                seen.append(w)
        return f"{question} {' '.join(seen)}".strip()

    async def _recall_window(self, limit: int, datasource: str) -> tuple[int, int]:
        """(k, rerank_k):小库不裁剪(全量候选),大库按 limit 倍数裁;空库 (0, 0)。

        窗口必须按**全库**文档数算,不是按目标 kind 的条数:通道的 top-k 是在
        全库上取的,按 kind 计数会低估候选池,让占比小的 kind 被挤空。
        """
        total = await self._store.count(datasource)
        if total <= 0:
            return 0, 0
        if total <= _EXHAUSTIVE_SCAN_MAX:
            return total, total
        budget = max(limit * _RECALL_MULTIPLIER, _RECALL_MIN)
        return budget, budget * 2

    async def _recall(
        self, kinds: tuple[str, ...], question: str, datasource: str, limit: int,
    ) -> tuple[list[tuple[int, dict]], dict[int, float]]:
        """Recall from the hybrid store, map doc_id → kb_items, filter by kind.

        候选窗口按库规模自适应(见 :meth:`_recall_window`):小库召回全量,下游
        确定性门看到的是**全部**候选,hybrid 与 builtin 的差别只剩排序信号
        (检索分进 ``_fuse_extra_sim``),不再有"裁掉本该命中的例子"这一项。
        """
        try:
            recall_k, pool_k = await self._recall_window(limit, datasource)
            if not recall_k:
                return [], {}
            hits = await self._store.recall(
                question, k=recall_k, rerank_k=pool_k,
                datasource=datasource,
                keyword_text=await self._expand_keyword(question, datasource),
            )
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
