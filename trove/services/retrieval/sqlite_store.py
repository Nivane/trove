"""SQLite fallback hybrid store (FTS5 sparse + in-Python cosine dense).

Used when no PostgreSQL retrieval DB is configured (non-postgres datasources
and test environments). Mirrors ``PgHybridStore``'s interface so the rest of
the stack is backend-agnostic. Scale is single-datasource KB size — not a
substitute for the PostgreSQL ANN path in production.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import aiosqlite

from trove.core.logging import get_logger
from trove.services.kb.backends.dense import cosine
from trove.services.kb.backends.fts import fts_index_text, fts_query
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
#: 列;v3 = 权威分 authority 列;v4 = 清除 doc_fts 孤儿行(旧重索引路径
#: 只删 documents 不删 doc_fts,孤儿 join 不回主子行,但永久占着 FTS 索引)。
#: v1 里**不含** sparse/authority 是刻意的:那是 v1 当年的真实形状,新库和
#: 存量库因此走同一条路径(存量库的 CREATE 是空操作,列由后续版本补上)。
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
    Migration(version=3, description="authority 权威分列",
              ops=[AddColumn("documents", "authority",
                             "REAL DEFAULT 0.0", "DOUBLE PRECISION DEFAULT 0.0")]),
    Migration(version=4, description="清除 doc_fts 孤儿行",
              ops=["DELETE FROM doc_fts WHERE rowid NOT IN "
                   "(SELECT rowid FROM documents)"]),
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
        authority_alpha: float | None = None,
        recorder: Any | None = None,
    ) -> None:
        super().__init__(
            embedder, reranker, rrf_k=rrf_k,
            rrf_weights=rrf_weights,
            authority_alpha=authority_alpha, recorder=recorder)
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

    async def _delete_docs(
        self, db: aiosqlite.Connection, where: str, params: tuple,
    ) -> None:
        """删 documents 行前先按 rowid 删对应 doc_fts 行。

        主子行一删,rowid 就再也反查不到 —— 留下的 FTS 行是永不可达的
        孤儿(检索无感,但永久占索引且随重索引翻倍)。``where`` 是调用方
        硬编码的列条件片段,不含用户输入。
        """
        await db.execute(
            f"DELETE FROM doc_fts WHERE rowid IN ("
            f"SELECT rowid FROM documents WHERE {where})", params)
        await db.execute(f"DELETE FROM documents WHERE {where}", params)

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
                await self._delete_docs(db, "doc_id = ?", (doc_id,))
                cur = await db.execute(
                    "INSERT INTO documents (doc_id, datasource, kind, source_file, content, embedding, authority) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (doc_id, doc.datasource, doc.kind, doc.source_file, doc.content,
                     _pack(emb), float(doc.authority or 0.0)),
                )
                rowid = cur.lastrowid
                # FTS 文本按写侧约定预分词(fts_index_text):与查询侧
                # fts_query 成对 —— CJK 两侧同拆单字符才有一致命中
                # (unicode61 把连续 CJK 视作一个 token)。
                await db.execute(
                    "INSERT INTO doc_fts (rowid, content) VALUES (?, ?)",
                    (rowid, fts_index_text(doc.content)),
                )
            await db.commit()

    async def delete_source(self, datasource: str, source_file: str) -> None:
        await self._ensure()
        async with aiosqlite.connect(self._db) as db:
            await self._delete_docs(
                db, "datasource = ? AND source_file = ?",
                (datasource, source_file))
            await db.commit()

    async def delete_kind(self, datasource: str, kind: str) -> None:
        await self._ensure()
        async with aiosqlite.connect(self._db) as db:
            await self._delete_docs(
                db, "datasource = ? AND kind = ?", (datasource, kind))
            await db.commit()

    async def clear(self, datasource: str) -> None:
        await self._ensure()
        async with aiosqlite.connect(self._db) as db:
            await self._delete_docs(db, "datasource = ?", (datasource,))
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
        # MATCH 串必须经 fts_query 归一(引号包 token、OR 连接、CJK 拆单字):
        # 与索引侧 fts_index_text 同一套预分词,写读两侧对齐才有一致命中;
        # 引号包裹也让问句里的 FTS5 语法字符(? * 括号)进不了语法。
        # 曾在这里对整串 .lower():FTS5 的 OR 是大写关键字,小写后析取
        # 变成对字面词 "or" 的合取,任何多词查询恒 0 命中。
        terms = fts_query(text)
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
                f"SELECT doc_id, content, kind, authority FROM documents "
                f"WHERE doc_id IN ({ph})",
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
                score=scores.get(doc_id, 0.0), kind=r["kind"],
                authority=float(r["authority"] or 0.0)))
        return out
