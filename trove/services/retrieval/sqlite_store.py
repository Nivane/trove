"""SQLite fallback hybrid store (FTS5 sparse + in-Python cosine dense).

Used when no PostgreSQL retrieval DB is configured (non-postgres datasources
and test environments). Mirrors ``PgHybridStore``'s interface so the rest of
the stack is backend-agnostic. Scale is single-datasource KB size — not a
substitute for the PostgreSQL ANN path in production.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

import aiosqlite

from trove.core.logging import get_logger
from trove.services.kb.backends.dense import cosine
from trove.services.retrieval.store import HybridStore, RetrievalDoc, RetrievalHit

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT UNIQUE NOT NULL,
    datasource TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_file TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    embedding BLOB
);
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(content, content_rowid);
CREATE INDEX IF NOT EXISTS idx_documents_ds ON documents(datasource);
"""


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *[float(x) for x in vec])


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


class SqliteHybridStore(HybridStore):
    def __init__(
        self, db_path: str | Path, embedder, reranker, dims: int = 0,
    ) -> None:
        super().__init__(embedder, reranker)
        self._db = str(db_path)
        self._dims = dims

    @classmethod
    def for_home(cls, home: str | Path, embedder, reranker) -> "SqliteHybridStore":
        p = Path(home) / "retrieval" / "retrieval.sqlite"
        p.parent.mkdir(parents=True, exist_ok=True)
        return cls(p, embedder, reranker)

    async def _ensure(self) -> None:
        async with aiosqlite.connect(self._db) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def index(self, doc: RetrievalDoc) -> None:
        await self.index_many([doc])

    async def index_many(self, docs: list[RetrievalDoc]) -> None:
        await self._ensure()
        async with aiosqlite.connect(self._db) as db:
            for doc in docs:
                doc_id = doc.item_key or f"{doc.kind}:{doc.source_file}"
                emb = doc.embedding or await self._embed(doc.content)
                if self._dims and len(emb) != self._dims:
                    emb = emb[: self._dims] + [0.0] * max(0, self._dims - len(emb))
                await db.execute(
                    "DELETE FROM documents WHERE doc_id = ?", (doc_id,))
                cur = await db.execute(
                    "INSERT INTO documents (doc_id, datasource, kind, source_file, content, embedding) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (doc_id, doc.datasource, doc.kind, doc.source_file, doc.content, _pack(emb)),
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

    async def _load(self, doc_ids: list[str]) -> list[RetrievalHit]:
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
        order = {d: i for i, d in enumerate(doc_ids)}
        out = []
        for doc_id in doc_ids:
            r = rows.get(doc_id)
            if r is None:
                continue
            out.append(RetrievalHit(
                doc_id=doc_id, content=r["content"], score=1.0 / (1 + order[doc_id]),
                kind=r["kind"]))
        return out
