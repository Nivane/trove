"""SQLite fallback hybrid store (FTS5 sparse + in-Python cosine dense).

Used when no PostgreSQL retrieval DB is configured (non-postgres datasources
and test environments). Mirrors ``PgHybridStore``'s interface so the rest of
the stack is backend-agnostic. Scale is single-datasource KB size — not a
substitute for the PostgreSQL ANN path in production.
"""

from __future__ import annotations

import struct
from pathlib import Path

import aiosqlite

from trove.core.logging import get_logger
from trove.services.kb.backends.dense import cosine
from trove.services.retrieval.store import HybridStore, RetrievalDoc, RetrievalHit
from trove.storage.migrations import (
    SQLITE,
    AddColumn,
    Migration,
    apply_migrations,
)

logger = get_logger(__name__)

_DOCUMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS documents (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT UNIQUE NOT NULL,
    datasource TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_file TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    embedding BLOB
)"""
_DOC_FTS = "CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(content, content_rowid)"
_DOC_DS_INDEX = "CREATE INDEX IF NOT EXISTS idx_documents_ds ON documents(datasource)"

#: 检索库的表结构版本。v1 = documents/doc_fts/索引;v2 = 稀疏通道的 sparse
#: 列。v1 里**不含** sparse 是刻意的:那是 v1 当年的真实形状,新库和存量库
#: 因此走同一条路径(存量库的 CREATE 是空操作,列由 v2 补上)。
#:
#: **v2 保留,即使 learned-sparse 通道已退役**:版本号记的是历史,不是配置。
#: 从列表里删掉 v2 会让存量库(停在 v2)看起来"比代码新"而触发
#: ``StorageSchemaTooNew`` —— 老代码开着新库是最安静的那种分歧。列留着不用
#: (新行写 NULL),退役靠代码停止读写,不靠迁移删列。
RETRIEVAL_MIGRATIONS = [
    Migration(version=1, description="documents 表与 FTS 镜像",
              ops=[_DOCUMENTS_TABLE, _DOC_FTS, _DOC_DS_INDEX]),
    Migration(version=2, description="sparse 稀疏通道列",
              ops=[AddColumn("documents", "sparse", "BLOB", "BYTEA")]),
]


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *[float(x) for x in vec])


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


class SqliteHybridStore(HybridStore):
    def __init__(
        self, db_path: str | Path, embedder, reranker, dims: int = 0,
        rrf_k: int = 60,
        rrf_weights: dict[str, float] | None = None,
        recorder: Any | None = None,
    ) -> None:
        super().__init__(
            embedder, reranker, rrf_k=rrf_k,
            rrf_weights=rrf_weights, recorder=recorder)
        self._db = str(db_path)
        self._dims = dims

    @classmethod
    def for_home(cls, home: str | Path, embedder, reranker, **kw) -> "SqliteHybridStore":
        p = Path(home) / "retrieval" / "retrieval.sqlite"
        p.parent.mkdir(parents=True, exist_ok=True)
        return cls(p, embedder, reranker, **kw)

    async def _ensure(self) -> None:
        async with aiosqlite.connect(self._db) as db:
            await apply_migrations(
                db, "retrieval", RETRIEVAL_MIGRATIONS, dialect=SQLITE)

    async def index(self, doc: RetrievalDoc) -> None:
        await self.index_many([doc])

    async def index_many(self, docs: list[RetrievalDoc]) -> None:
        await self._ensure()
        async with aiosqlite.connect(self._db) as db:
            for doc in docs:
                doc_id = doc.item_key or f"{doc.kind}:{doc.source_file}"
                emb = doc.embedding
                if emb is None:
                    emb = await self._embed(doc.content)
                if self._dims and len(emb) != self._dims:
                    emb = emb[: self._dims] + [0.0] * max(0, self._dims - len(emb))
                await db.execute(
                    "DELETE FROM documents WHERE doc_id = ?", (doc_id,))
                cur = await db.execute(
                    "INSERT INTO documents (doc_id, datasource, kind, source_file, content, embedding) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (doc_id, doc.datasource, doc.kind, doc.source_file, doc.content,
                     _pack(emb)),
                )
                rowid = cur.lastrowid
                await db.execute(
                    "INSERT INTO doc_fts (rowid, content) VALUES (?, ?)",
                    (rowid, doc.content),
                )
            await db.commit()

    async def delete_source(self, datasource: str, source_file: str) -> None:
        await self._ensure()
        async with aiosqlite.connect(self._db) as db:
            await db.execute(
                "DELETE FROM doc_fts WHERE rowid IN ("
                "SELECT rowid FROM documents WHERE datasource = ? AND source_file = ?)",
                (datasource, source_file),
            )
            await db.execute(
                "DELETE FROM documents WHERE datasource = ? AND source_file = ?",
                (datasource, source_file),
            )
            await db.commit()

    async def delete_kind(self, datasource: str, kind: str) -> None:
        await self._ensure()
        async with aiosqlite.connect(self._db) as db:
            await db.execute(
                "DELETE FROM doc_fts WHERE rowid IN ("
                "SELECT rowid FROM documents WHERE datasource = ? AND kind = ?)",
                (datasource, kind),
            )
            await db.execute(
                "DELETE FROM documents WHERE datasource = ? AND kind = ?",
                (datasource, kind),
            )
            await db.commit()

    async def clear(self, datasource: str) -> None:
        await self._ensure()
        async with aiosqlite.connect(self._db) as db:
            await db.execute(
                "DELETE FROM doc_fts WHERE rowid IN ("
                "SELECT rowid FROM documents WHERE datasource = ?)",
                (datasource,),
            )
            await db.execute(
                "DELETE FROM documents WHERE datasource = ?", (datasource,))
            await db.commit()

    async def count(self, datasource: str) -> int:
        await self._ensure()
        async with aiosqlite.connect(self._db) as db:
            cur = await db.execute(
                "SELECT count(*) FROM documents WHERE datasource = ?",
                (datasource,),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def _fts_ids(self, text: str, k: int) -> list[str]:
        await self._ensure()
        terms = " ".join(t for t in text.lower().split() if t)
        if not terms:
            return []
        async with aiosqlite.connect(self._db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT d.doc_id FROM doc_fts f JOIN documents d "
                "ON d.rowid = f.rowid WHERE d.datasource = ? AND doc_fts MATCH ? "
                "ORDER BY bm25(doc_fts) LIMIT ?",
                (self._ds, terms, k),
            )
            rows = await cur.fetchall()
        return [r["doc_id"] for r in rows]

    async def _ann_ids(self, vector: list[float], k: int) -> list[str]:
        await self._ensure()
        async with aiosqlite.connect(self._db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT doc_id, embedding FROM documents "
                "WHERE datasource = ? AND embedding IS NOT NULL",
                (self._ds,),
            )
            rows = await cur.fetchall()
        scored = [
            (r["doc_id"], cosine(vector, _unpack(r["embedding"]))) for r in rows
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [doc_id for doc_id, _ in scored[:k]]

    async def _load(
        self, doc_ids: list[str], scores: dict[str, float],
    ) -> list[RetrievalHit]:
        if not doc_ids:
            return []
        await self._ensure()
        async with aiosqlite.connect(self._db) as db:
            db.row_factory = aiosqlite.Row
            ph = ",".join("?" * len(doc_ids))
            cur = await db.execute(
                f"SELECT doc_id, content, kind FROM documents WHERE doc_id IN ({ph})",
                doc_ids,
            )
            rows = {r["doc_id"]: r for r in await cur.fetchall()}
        out = []
        for doc_id in doc_ids:
            r = rows.get(doc_id)
            if r is None:
                continue
            out.append(RetrievalHit(
                doc_id=doc_id, content=r["content"],
                score=scores.get(doc_id, 0.0), kind=r["kind"]))
        return out
