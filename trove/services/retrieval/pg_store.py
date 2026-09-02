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
"""

from __future__ import annotations

from typing import Any

from trove.core.logging import get_logger
from trove.services.kb.backends.dense import Embedder
from trove.services.retrieval.store import HybridStore, RetrievalDoc, RetrievalHit

logger = get_logger(__name__)

_SCHEMA_NS = "trove_retrieval"
_BM25_IDX = f"{_SCHEMA_NS}.documents_bm25"


class PgHybridStore(HybridStore):
    def __init__(
        self, dsn: str, embedder: Embedder | None, reranker: Any | None,
        dims: int = 1536, fts_tokenizer: str | None = None,
        sparse_dim: int = 0, rrf_k: int = 60,
        rrf_weights: dict[str, float] | None = None,
        recorder: Any | None = None,
    ) -> None:
        super().__init__(
            embedder, reranker, sparse_dim=sparse_dim, rrf_k=rrf_k,
            rrf_weights=rrf_weights, recorder=recorder)
        self._dsn = dsn
        self._dims = dims
        self._fts_tokenizer = fts_tokenizer
        self._ensured = False
        self._fts_mode: str = "bm25"

    @staticmethod
    def _lit(vec: list[float]) -> str:
        return "[" + ",".join(f"{float(x):.8f}" for x in vec) + "]"

    def _lit_sparse(self, sparse: dict[int, float]) -> str:
        if not sparse:
            return ""
        body = ",".join(f"{i}:{float(v):.6f}" for i, v in sorted(sparse.items()))
        return f"{{{body}}}/{self._sparse_dim}"

    async def _connect(self):
        import psycopg

        return await psycopg.AsyncConnection.connect(self._dsn)

    async def _ensure(self) -> None:
        if self._ensured:
            return
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA_NS}")
                await cur.execute(f"CREATE EXTENSION IF NOT EXISTS vector")
                sparse_col = (
                    f", sparse sparsevec({self._sparse_dim})" if self._sparse_dim else "")
                await cur.execute(
                    f"""CREATE TABLE IF NOT EXISTS {_SCHEMA_NS}.documents (
                        id TEXT PRIMARY KEY,
                        datasource TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        source_file TEXT NOT NULL DEFAULT '',
                        content TEXT NOT NULL,
                        tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
                        embedding vector({self._dims})
                        {sparse_col}
                    )""")
                if self._sparse_dim:
                    # 既有表没有 sparse 列时补列(幂等)+ HNSW 索引。
                    await cur.execute(
                        f"ALTER TABLE {_SCHEMA_NS}.documents "
                        f"ADD COLUMN IF NOT EXISTS sparse sparsevec({self._sparse_dim})")
                await cur.execute(
                    f"CREATE INDEX IF NOT EXISTS documents_ds ON "
                    f"{_SCHEMA_NS}.documents(datasource)")
                await cur.execute(
                    f"CREATE INDEX IF NOT EXISTS documents_vec ON "
                    f"{_SCHEMA_NS}.documents USING hnsw (embedding vector_cosine_ops)")
                if self._sparse_dim:
                    await cur.execute(
                        f"CREATE INDEX IF NOT EXISTS documents_sparse ON "
                        f"{_SCHEMA_NS}.documents "
                        f"USING hnsw (sparse sparsevec_cosine_ops)")
                # Sparse channel: prefer pg_bm25 (true BM25), else tsvector GIN.
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
                    sparse = None
                    if emb is None:
                        if hasattr(self._embedder, "embed_hybrid"):
                            emb, sparse = (
                                await self._embedder.embed_hybrid([doc.content]))[0]
                        else:
                            emb = await self._embed(doc.content)
                            if self._sparse_dim and hasattr(self._embedder, "embed_sparse"):
                                sparse = (
                                    await self._embedder.embed_sparse([doc.content]))[0]
                    if len(emb) != self._dims:
                        # pad/truncate to declared dims for a stable index
                        if len(emb) < self._dims:
                            emb = emb + [0.0] * (self._dims - len(emb))
                        else:
                            emb = emb[: self._dims]
                    sparse_lit = (
                        self._lit_sparse(sparse) if sparse and self._sparse_dim else None)
                    if self._sparse_dim:
                        await cur.execute(
                            f"DELETE FROM {_SCHEMA_NS}.documents WHERE id = %s", (doc_id,))
                        await cur.execute(
                            f"""INSERT INTO {_SCHEMA_NS}.documents
                            (id, datasource, kind, source_file, content, embedding, sparse)
                            VALUES (%s, %s, %s, %s, %s, %s::vector, %s::sparsevec)""",
                            (doc_id, doc.datasource, doc.kind, doc.source_file,
                             doc.content, self._lit(emb), sparse_lit),
                        )
                    else:
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

    async def _sparse_ann_ids(self, sparse: dict[int, float], k: int) -> list[str]:
        if not sparse or not self._sparse_dim:
            return []
        await self._ensure()
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""SELECT id FROM {_SCHEMA_NS}.documents
                    WHERE datasource = %s AND sparse IS NOT NULL
                    ORDER BY sparse <=> %s::sparsevec LIMIT %s""",
                    (self._ds, self._lit_sparse(sparse), k),
                )
                rows = await cur.fetchall()
            return [r[0] for r in rows]
        finally:
            await conn.close()

    async def _load(self, doc_ids: list[str]) -> list[RetrievalHit]:
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
        order = {d: i for i, d in enumerate(doc_ids)}
        out = []
        for doc_id in doc_ids:
            r = by_id.get(doc_id)
            if r is None:
                continue
            out.append(RetrievalHit(
                doc_id=doc_id, content=r[1], score=1.0 / (1 + order[doc_id]),
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
