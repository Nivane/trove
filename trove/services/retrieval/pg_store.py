"""PostgreSQL hybrid store — pg_bm25 (ParadeDB) + pgvector ANN (HNSW) + RRF.

Production retrieval backend. A single PostgreSQL DB (``retrieval_dsn``) holds
both the sparse ``pg_bm25`` channel (true BM25, jieba/icu tokenizers) and the
dense ``pgvector`` channel (cosine, HNSW-indexed). ``recall`` fuses them with
RRF and a pluggable reranker does the fine (精排) pass.

Index health: the HNSW index is created lazily on first ``index`` when the
dimension is known. The BM25 index is created in ``_ensure``; if the ``pg_bm25``
extension is unavailable (non-ParadeDB image) the store degrades gracefully to a
native ``tsvector`` GIN index so the rest of the pipeline keeps working. Schema
lives in the ``trove_retrieval`` schema so it never collides with business
tables.

存量库可能残留已退役的 learned-sparse 列(``sparse`` + ``documents_sparse``
索引):那是由配置驱动的可选列,按 ``storage/migrations`` 的规则**不占版本号、
也不做破坏性迁移** —— 代码停止读写即可,列留原地(新行写 NULL)。给新库建表
时不再创建它。
"""

from __future__ import annotations

from typing import Any

from trove.core.logging import get_logger
from trove.services.kb.backends.dense import Embedder
from trove.services.retrieval.store import HybridStore, RetrievalDoc, RetrievalHit
from trove.storage.migrations import (
    POSTGRES,
    Migration,
    apply_migrations,
)

logger = get_logger(__name__)

_SCHEMA_NS = "trove_retrieval"
_BM25_IDX = f"{_SCHEMA_NS}.documents_bm25"


class PgHybridStore(HybridStore):
    def __init__(
        self, dsn: str, embedder: Embedder | None, reranker: Any | None,
        dims: int = 1536, fts_tokenizer: str | None = None,
        rrf_k: int = 60,
        rrf_weights: dict[str, float] | None = None,
        recorder: Any | None = None,
    ) -> None:
        super().__init__(
            embedder, reranker, rrf_k=rrf_k,
            rrf_weights=rrf_weights, recorder=recorder)
        self._dsn = dsn
        self._dims = dims
        self._fts_tokenizer = fts_tokenizer
        self._ensured = False
        self._fts_mode: str = "bm25"

    @staticmethod
    def _lit(vec: list[float]) -> str:
        return "[" + ",".join(f"{float(x):.8f}" for x in vec) + "]"

    async def _connect(self):
        import psycopg

        return await psycopg.AsyncConnection.connect(self._dsn)

    def _base_migrations(self) -> list[Migration]:
        """v1 = 表与索引。

        **版本号与配置无关**:向量维度是配置,配置不该让库"变新"(见
        ``storage/migrations`` 模块文档)。退役的 sparse 列当年走的也是
        "配置驱动、不占版本号"那条路,所以它同样不进 v1 —— 存量库里的残留列
        由代码停止读写来退役,不靠迁移删。
        """
        return [Migration(version=1, description="documents 表与索引", ops=[
            f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA_NS}",
            "CREATE EXTENSION IF NOT EXISTS vector",
            f"""CREATE TABLE IF NOT EXISTS {_SCHEMA_NS}.documents (
                        id TEXT PRIMARY KEY,
                        datasource TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        source_file TEXT NOT NULL DEFAULT '',
                        content TEXT NOT NULL,
                        tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
                        embedding vector({self._dims})
                    )""",
            f"CREATE INDEX IF NOT EXISTS documents_ds ON "
            f"{_SCHEMA_NS}.documents(datasource)",
            f"CREATE INDEX IF NOT EXISTS documents_vec ON "
            f"{_SCHEMA_NS}.documents USING hnsw (embedding vector_cosine_ops)",
        ])]

    async def _ensure(self) -> None:
        if self._ensured:
            return
        conn = await self._connect()
        try:
            await apply_migrations(
                conn, "retrieval", self._base_migrations(), dialect=POSTGRES)
            async with conn.cursor() as cur:
                # Keyword channel: prefer pg_bm25 (true BM25), else tsvector GIN.
                # 用 savepoint 包裹 bm25 尝试:CREATE EXTENSION 失败会把整个事务
                # 置为 aborted,后续 GIN 回退也报 InFailedSqlTransaction——回滚到
                # savepoint 恢复事务状态后再建 GIN 索引。
                try:
                    await cur.execute("SAVEPOINT sp_bm25")
                    await cur.execute("CREATE EXTENSION IF NOT EXISTS pg_bm25")
                    opts = "content = 'text'"
                    if self._fts_tokenizer:
                        opts += f", content_tokenizer = '{self._fts_tokenizer}'"
                    await cur.execute(
                        f"DROP INDEX IF EXISTS {_BM25_IDX}")
                    await cur.execute(
                        f"CREATE INDEX {_BM25_IDX} ON {_SCHEMA_NS}.documents "
                        f"USING bm25 (id, content) WITH ({opts})")
                    await cur.execute("RELEASE SAVEPOINT sp_bm25")
                    self._fts_mode = "bm25"
                except Exception as e:  # pg_bm25 not available -> native FTS
                    logger.warning("pg_bm25 unavailable (%s); using tsvector GIN", e)
                    await cur.execute("ROLLBACK TO SAVEPOINT sp_bm25")
                    await cur.execute(
                        f"CREATE INDEX IF NOT EXISTS documents_tsv ON "
                        f"{_SCHEMA_NS}.documents USING GIN(tsv)")
                    self._fts_mode = "tsvector"
            await conn.commit()
        finally:
            await conn.close()
        self._ensured = True

    async def index(self, doc: RetrievalDoc) -> None:
        await self.index_many([doc])

    async def index_many(self, docs: list[RetrievalDoc]) -> None:
        await self._ensure()
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                for doc in docs:
                    doc_id = doc.item_key or f"{doc.kind}:{doc.source_file}"
                    emb = doc.embedding
                    if emb is None:
                        emb = await self._embed(doc.content)
                    if len(emb) != self._dims:
                        # pad/truncate to declared dims for a stable index
                        if len(emb) < self._dims:
                            emb = emb + [0.0] * (self._dims - len(emb))
                        else:
                            emb = emb[: self._dims]
                    await cur.execute(
                        f"DELETE FROM {_SCHEMA_NS}.documents WHERE id = %s", (doc_id,))
                    await cur.execute(
                        f"""INSERT INTO {_SCHEMA_NS}.documents
                        (id, datasource, kind, source_file, content, embedding)
                        VALUES (%s, %s, %s, %s, %s, %s::vector)""",
                        (doc_id, doc.datasource, doc.kind, doc.source_file,
                         doc.content, self._lit(emb)),
                    )
            await conn.commit()
        finally:
            await conn.close()

    async def delete_source(self, datasource: str, source_file: str) -> None:
        await self._ensure()
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM {_SCHEMA_NS}.documents "
                    "WHERE datasource = %s AND source_file = %s",
                    (datasource, source_file))
            await conn.commit()
        finally:
            await conn.close()

    async def delete_kind(self, datasource: str, kind: str) -> None:
        await self._ensure()
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM {_SCHEMA_NS}.documents "
                    "WHERE datasource = %s AND kind = %s",
                    (datasource, kind))
            await conn.commit()
        finally:
            await conn.close()

    async def clear(self, datasource: str) -> None:
        await self._ensure()
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM {_SCHEMA_NS}.documents WHERE datasource = %s",
                    (datasource,))
            await conn.commit()
        finally:
            await conn.close()

    async def count(self, datasource: str) -> int:
        await self._ensure()
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT count(*) FROM {_SCHEMA_NS}.documents "
                    "WHERE datasource = %s",
                    (datasource,),
                )
                row = await cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            await conn.close()

    async def _fts_ids(self, text: str, k: int) -> list[str]:
        await self._ensure()
        terms = " ".join(t for t in text.lower().split() if t)
        if not terms:
            return []
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                if self._fts_mode == "bm25":
                    await cur.execute(
                        f"""SELECT id FROM {_SCHEMA_NS}.documents
                        WHERE datasource = %s
                          AND bm25_search('{_BM25_IDX}', %s) IS NOT NULL
                        ORDER BY bm25_search('{_BM25_IDX}', %s) DESC
                        LIMIT %s""",
                        (self._ds, terms, terms, k),
                    )
                else:
                    await cur.execute(
                        f"""SELECT id FROM {_SCHEMA_NS}.documents
                        WHERE datasource = %s
                          AND tsv @@ plainto_tsquery('simple', %s)
                        ORDER BY ts_rank(tsv, plainto_tsquery('simple', %s)) DESC
                        LIMIT %s""",
                        (self._ds, terms, terms, k),
                    )
                rows = await cur.fetchall()
            return [r[0] for r in rows]
        finally:
            await conn.close()

    async def _ann_ids(self, vector: list[float], k: int) -> list[str]:
        await self._ensure()
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""SELECT id FROM {_SCHEMA_NS}.documents
                    WHERE datasource = %s
                    ORDER BY embedding <=> %s::vector LIMIT %s""",
                    (self._ds, self._lit(vector), k),
                )
                rows = await cur.fetchall()
            return [r[0] for r in rows]
        finally:
            await conn.close()

    async def _load(
        self, doc_ids: list[str], scores: dict[str, float],
    ) -> list[RetrievalHit]:
        if not doc_ids:
            return []
        await self._ensure()
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT id, content, kind FROM {_SCHEMA_NS}.documents "
                    "WHERE id = ANY(%s)",
                    (doc_ids,),
                )
                rows = await cur.fetchall()
            by_id = {r[0]: r for r in rows}
        finally:
            await conn.close()
        out = []
        for doc_id in doc_ids:
            r = by_id.get(doc_id)
            if r is None:
                continue
            out.append(RetrievalHit(
                doc_id=doc_id, content=r[1], score=scores.get(doc_id, 0.0),
                kind=r[2]))
        return out

    # datasource is threaded through recall() via self._ds (set in base recall).
    async def recall(
        self, query: str, k: int = 20, rerank_k: int = 40,
        datasource: str = "", keyword_text: str | None = None,
        return_meta: bool = False,
    ) -> list[RetrievalHit] | tuple[list[RetrievalHit], dict]:
        return await super().recall(
            query, k=k, rerank_k=rerank_k, datasource=datasource,
            keyword_text=keyword_text, return_meta=return_meta)
