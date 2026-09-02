"""Episodic memory — cross-session "what was queried/resulted" recall.

Episodes are the automatic cross-session layer: every executed query run
lands one row (question → SQL → verdict → result shape → corrections),
deduplicated by ``(datasource, user_id, question, sql)``. Retrieval is
**hybrid by default**: lexical (``relevance_score`` gate, zero LLM) fused
with a cosine channel when an embedder is available for the datasource
(``embedder_backend`` / ``embedding_model`` in datasources.yml) — each
episode stores its question+matched_tables+correction_history embedding
as a BLOB column. The relaxed gate (lexical ≥ 0.5 **OR** cosine ≥ 0.55)
keeps lexical-only deployments unchanged (regression-safe: no embedder →
pure lexical path, exactly the old behavior).

Episodes only inject into generation as an *optional* context block, so
past similar queries become a hint, not an answer source. This is not
conversation history: history lives in the session store; episodes are
distilled, scoped, retrievable *facts about what happened*.
"""

from __future__ import annotations

import json
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import aiosqlite

from trove.core.logging import get_logger
from trove.services.memory.models import MemoryEntry, MemoryScope

logger = get_logger(__name__)

EPISODES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    datasource TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL,
    sql TEXT NOT NULL DEFAULT '',
    dialect TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL DEFAULT '',
    row_count INTEGER NOT NULL DEFAULT -1,
    result_signature TEXT NOT NULL DEFAULT '',
    correction_history TEXT NOT NULL DEFAULT '[]',
    matched_tables TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
# 检索时只扫用户自己的 episode,避免把别人的口径/结果当上下文。
_EPISODE_UNIQ = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_uniq
ON episodes(datasource, user_id, question, sql)
"""
_EPISODE_SCOPE = """
CREATE INDEX IF NOT EXISTS idx_episodes_scope
ON episodes(datasource, user_id, updated_at)
"""

DEFAULT_EPISODE_LIMIT = 2
# ⑤ hybrid 检索:候选窗口 200 → 500(向量通道提升跨措辞召回,窗口放宽
# 摊平在每行几微秒的余弦计算上);融合权重与放宽门限:
_SEARCH_WINDOW = 500
_COSINE_WEIGHT = 0.6          # 语义通道权重(向量可用时)
_LEXICAL_WEIGHT = 0.4         # 词面通道权重(始终可用)
_LEXICAL_GATE = 0.5           # 词面门槛(纯词面部署 = 旧行为)
_COSINE_GATE = 0.55           # 语义门槛:OR 语义——任一门过即召回

_EMBED_COL_SQLITE = "ALTER TABLE episodes ADD COLUMN embedding BLOB"
_EMBED_COL_PG = "ALTER TABLE episodes ADD COLUMN IF NOT EXISTS embedding BYTEA"


def _pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack_embedding(blob: bytes | None) -> list[float] | None:
    if not blob:
        return None
    try:
        n = len(blob) // 4
        return list(struct.unpack(f"<{n}f", blob))
    except (struct.error, TypeError):
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度;零向量或维度不一致 → 0(退化为纯词面通道)。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EpisodeStore:
    """Episodic memory store (``~/.trove/memory/episodes.sqlite``, StorageBackend-backed).

    ``embedder_factory``: ``(datasource) -> embedder | None`` —— 数据源级
    复用 KB 的 ``build_embedder`` 配置(bge-m3 本地 / LLM 网关)。返回
    None 的数据源走纯词面路径(旧行为不变)。嵌入写入与检索均 best-effort:
    嵌入失败绝不阻塞 episode 记录或检索。
    """

    def __init__(
        self,
        db_path: str | Path,
        embedder_factory: Callable[[str], Any] | None = None,
    ):
        self.db_path = Path(db_path)
        from trove.storage.backends import resolve_backend

        self._backend = resolve_backend(str(db_path))
        self._embedder_factory = embedder_factory
        self._schema_ready = False

    async def _conn(self):
        await self._ensure_schema()
        return self._backend

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        from trove.storage.backends.base import script_statements

        await self._backend.executescript(
            script_statements([EPISODES_TABLE_SQL, _EPISODE_UNIQ, _EPISODE_SCOPE])
        )
        # 幂等加列:老库升级路径(SQLite BLOB / PG BYTEA);已存在 → 忽略
        # (两后端对重复列分别抛 OperationalError / ProgrammingError)。
        is_pg = "Postgres" in type(self._backend).__name__
        try:
            await self._backend.execute(
                _EMBED_COL_PG if is_pg else _EMBED_COL_SQLITE)
            await self._backend.commit()
        except Exception:
            pass
        self._schema_ready = True

    def _embedder(self, datasource: str) -> Any | None:
        if self._embedder_factory is None:
            return None
        try:
            return self._embedder_factory(datasource)
        except Exception as e:
            logger.warning("Embedder factory failed for %s: %s", datasource, e)
            return None

    def _embed_text(self, question: str, matched_tables: list[str],
                    correction_history: list[str]) -> str:
        """嵌入/词面共用的检索文本:问题 + 命中表 + 修正史(⑤ 起修正史入检索)。

        空表/空修正史会残留尾随空格——strip,保证精确查表型 embedder
        的键一致性(写入与检索同文本)。
        """
        return " ".join([
            question,
            " ".join(matched_tables or []),
            " ".join(correction_history or []),
        ]).strip()

    async def _try_embed(self, embedder: Any | None, text: str) -> bytes | None:
        if embedder is None or not text.strip():
            return None
        try:
            vec = await embedder.embed([text[:1000]])
            if vec and vec[0]:
                return _pack_embedding(vec[0])
        except Exception as e:
            logger.warning("Episode embed failed (best-effort): %s", e)
        return None

    # ── Write ─────────────────────────────────────────────

    async def record(
        self,
        scope: MemoryScope,
        *,
        session_id: str = "",
        run_id: str = "",
        question: str,
        sql: str = "",
        dialect: str = "",
        verdict: str = "",
        row_count: int = -1,
        result_signature: str = "",
        correction_history: list[str] | None = None,
        matched_tables: list[str] | None = None,
    ) -> None:
        """Insert (or refresh) one episode; idempotent by scope+question+sql.

        Equal-text reruns refresh ``updated_at`` (recency signal) instead of
        piling duplicates — same idempotent-conflict-resolution philosophy as
        user facts.
        """
        question = (question or "").strip()
        sql = (sql or "").strip()
        if not question or not scope.datasource:
            return
        ts = now_iso()
        # ⑤ 写入时嵌入(问题 + 命中表 + 修正史),best-effort——嵌入失败
        # 只丢向量通道,不阻塞 episode 记录本身。
        matched = matched_tables or []
        corrections = correction_history or []
        embedding = await self._try_embed(
            self._embedder(scope.datasource),
            self._embed_text(question, matched, corrections),
        )
        conn = await self._conn()
        try:
            await conn.execute(
                "INSERT INTO episodes (user_id, datasource, session_id, run_id, "
                "question, sql, dialect, verdict, row_count, result_signature, "
                "correction_history, matched_tables, embedding, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(datasource, user_id, question, sql) DO UPDATE SET "
                "verdict=excluded.verdict, row_count=excluded.row_count, "
                "result_signature=excluded.result_signature, "
                "correction_history=excluded.correction_history, "
                "matched_tables=excluded.matched_tables, "
                "embedding=excluded.embedding, "
                "dialect=excluded.dialect, updated_at=excluded.updated_at",
                (
                    scope.user_id, scope.datasource, session_id, run_id,
                    question, sql, dialect, verdict, row_count,
                    result_signature,
                    json.dumps(corrections, ensure_ascii=False),
                    json.dumps(matched, ensure_ascii=False),
                    embedding, ts, ts,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def touch(self, scope: MemoryScope, question: str, sql: str = "") -> None:
        """Refresh last-used timestamp on read (episode lifecycle signal)."""
        conn = await self._conn()
        try:
            await conn.execute(
                "UPDATE episodes SET updated_at = ? "
                "WHERE datasource = ? AND user_id = ? AND question = ? AND sql = ?",
                (now_iso(), scope.datasource, scope.user_id, question, sql),
            )
            await conn.commit()
        finally:
            await conn.close()

    # ── Read ──────────────────────────────────────────────

    async def search(
        self, scope: MemoryScope, question: str,
        limit: int = DEFAULT_EPISODE_LIMIT,
    ) -> list[MemoryEntry]:
        """Hybrid-gate retrieval: lexical ≥ 0.5 OR cosine ≥ 0.55, top-N.

        ⑤ 混合通道:候选行带 embedding 且查询能嵌入 → 双通道评分
        (score = 0.6·cos + 0.4·lexical);任一侧不可用 → 纯词面路径
        (旧行为,回归安全)。修正史并入检索文本,增强跨措辞召回。
        Returns memory entries scored for the context budget (item-level
        trim). Reads only the user's own episodes for this datasource.
        """
        from trove.workflow.context_score import relevance_score

        embedder = self._embedder(scope.datasource)
        # 先解包成 list 再参与余弦(打包 bytes 的 len 是字节数,直接传会
        # 被 _cosine 的维度检查拦成 0——曾经的真实 bug)。
        query_vec = None
        if embedder:
            raw = await self._try_embed(embedder, question)
            query_vec = _unpack_embedding(raw) if raw else None

        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT id, session_id, run_id, question, sql, dialect, verdict, "
                "row_count, result_signature, correction_history, matched_tables, "
                "embedding, created_at, updated_at "
                "FROM episodes WHERE datasource = ? AND user_id = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (scope.datasource, scope.user_id, _SEARCH_WINDOW),
            )
            rows = await cursor.fetchall()
        finally:
            await conn.close()

        if not rows:
            return []

        def _decay(updated_at: str) -> float:
            try:
                dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
            except Exception:
                return 0.0
            return 1.0 / (1.0 + days / 30.0)  # 30 天半衰的温和最近度

        out: list[MemoryEntry] = []
        for r in rows:
            # 列序:0 id,1 session_id,2 run_id,3 question,4 sql,5 dialect,
            # 6 verdict,7 row_count,8 result_signature,9 correction_history,
            # 10 matched_tables,11 embedding,12 created_at,13 updated_at
            q_text = str(r[3] or "")
            sql = str(r[4] or "")
            matched = _json_list(r[10])
            corrections = _json_list(r[9])
            # 检索文本 = 问题 + 命中表 + 修正史(候选侧),只对查询问题算,
            # 与库里的问题无关——否则每条 episode 都自匹配(regression 约束)。
            text = self._embed_text(q_text, matched, corrections)
            rel = relevance_score(text, question)

            cos = 0.0
            if query_vec is not None:
                row_vec = _unpack_embedding(r[11])
                if row_vec:
                    cos = _cosine(query_vec, row_vec)
            if rel < _LEXICAL_GATE and cos < _COSINE_GATE:
                continue  # 放宽门:任一门过即召回(纯词面部署 = 旧门槛)
            score = (
                _COSINE_WEIGHT * cos + _LEXICAL_WEIGHT * rel
                if query_vec is not None else rel
            ) + 0.2 * _decay(r[13])
            out.append(MemoryEntry(
                kind="episode",
                scope=scope,
                content={
                    "question": q_text, "sql": sql, "dialect": r[5],
                    "verdict": r[6], "row_count": r[7],
                    "result_signature": r[8],
                    "correction_history": corrections,
                    "matched_tables": matched,
                    "session_id": r[1], "run_id": r[2],
                },
                source="auto", confidence=1.0 if r[6] == "OK" else 0.4,
                status="confirmed",
                score=round(score, 4),
                created_at=r[12], updated_at=r[13],
                # ⑦ touch 契约:idempotency_key = "question\x1fsql",读路径
                # 命中后按此键刷新最近使用时间(生命周期信号)。
                idempotency_key=f"{q_text}\x1f{sql}",
            ))
        out.sort(key=lambda e: e.score, reverse=True)
        return out[:limit]

    # ── Lifecycle ─────────────────────────────────────────

    async def purge(self, retention_days: int | None) -> int:
        """Physical purge of episodes older than ``retention_days`` (None=keep)."""
        if not retention_days:
            return 0
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "DELETE FROM episodes WHERE updated_at < ?", (cutoff.isoformat(),))
            await conn.commit()
            return cursor.rowcount
        finally:
            await conn.close()

    async def count(self, scope: MemoryScope | None = None) -> int:
        conn = await self._conn()
        try:
            if scope is None or not scope.datasource:
                cursor = await conn.execute("SELECT COUNT(*) FROM episodes")
            else:
                cursor = await conn.execute(
                    "SELECT COUNT(*) FROM episodes WHERE datasource = ? AND user_id = ?",
                    (scope.datasource, scope.user_id),
                )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
        finally:
            await conn.close()


def _json_list(raw: str) -> list[Any]:
    try:
        return json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []
