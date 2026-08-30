"""Episodic memory — cross-session "what was queried/resulted" recall.

Episodes are the automatic cross-session layer: every executed query run
lands one row (question → SQL → verdict → result shape → corrections),
deduplicated by ``(datasource, user_id, question, sql)``. Retrieval is
purely deterministic (``relevance_score`` gate + recency boost) — zero LLM,
zero vectors — and only injects into generation as an *optional* context
block, so past similar queries become a hint, not an answer source.

This is not conversation history: history lives in the session store;
episodes are distilled, scoped, retrievable *facts about what happened*.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EpisodeStore:
    """Episodic memory store (``~/.trove/memory/episodes.sqlite``, StorageBackend-backed)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        from trove.storage.backends import resolve_backend

        self._backend = resolve_backend(str(db_path))
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
        self._schema_ready = True

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
        conn = await self._conn()
        try:
            await conn.execute(
                "INSERT INTO episodes (user_id, datasource, session_id, run_id, "
                "question, sql, dialect, verdict, row_count, result_signature, "
                "correction_history, matched_tables, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(datasource, user_id, question, sql) DO UPDATE SET "
                "verdict=excluded.verdict, row_count=excluded.row_count, "
                "result_signature=excluded.result_signature, "
                "correction_history=excluded.correction_history, "
                "matched_tables=excluded.matched_tables, "
                "dialect=excluded.dialect, updated_at=excluded.updated_at",
                (
                    scope.user_id, scope.datasource, session_id, run_id,
                    question, sql, dialect, verdict, row_count,
                    result_signature,
                    json.dumps(correction_history or [], ensure_ascii=False),
                    json.dumps(matched_tables or [], ensure_ascii=False),
                    ts, ts,
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
        """Deterministic-gate retrieval: relevance_score ≥ 0.5, recency top-N.

        Returns memory entries scored for the context budget (item-level
        trim). Reads only the user's own episodes for this datasource.
        """
        from trove.workflow.context_score import relevance_score

        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT id, session_id, run_id, question, sql, dialect, verdict, "
                "row_count, result_signature, correction_history, matched_tables, "
                "created_at, updated_at "
                "FROM episodes WHERE datasource = ? AND user_id = ? "
                "ORDER BY updated_at DESC LIMIT 200",
                (scope.datasource, scope.user_id),
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
            # 10 matched_tables,11 created_at,12 updated_at
            q_text = str(r[3] or "")
            sql = str(r[4] or "")
            # 检索相关度只对"查询问题"算(候选 = 该 episode 的问题 + 命中表),
            # 与库里存的问题无关——否则每条 episode 都自匹配(regression 约束)。
            text = " ".join([q_text, str(r[10]) if r[10] else ""])
            rel = relevance_score(text, question)
            if rel < 0.5:
                continue
            score = rel + 0.2 * _decay(r[12])
            out.append(MemoryEntry(
                kind="episode",
                scope=scope,
                content={
                    "question": q_text, "sql": sql, "dialect": r[5],
                    "verdict": r[6], "row_count": r[7],
                    "result_signature": r[8],
                    "correction_history": _json_list(r[9]),
                    "matched_tables": _json_list(r[10]),
                    "session_id": r[1], "run_id": r[2],
                },
                source="auto", confidence=1.0 if r[6] == "OK" else 0.4,
                status="confirmed",
                score=round(score, 4),
                created_at=r[11], updated_at=r[12],
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
