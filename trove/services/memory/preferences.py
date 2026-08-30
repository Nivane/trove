"""Automatic user-preference memory — LLM draft → validate → confirm.

User facts today are explicit-only (``/facts add``). This module closes the
"no auto user-preference learning" gap: at a natural boundary (session
compaction), the LLM extracts candidate preference/caliber statements from
the recent conversation; high-confidence candidates are validated through
the existing ``UserFactsService`` write policy (short/self-contained/idempotent)
and committed directly; low-confidence ones land as *pending drafts* for a
human to confirm — never silently injected into generation.

Drafts live in ``~/.trove/memory/preferences.sqlite`` (independent of the
KB/user-facts stores so auto content never pollutes manual sources).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from trove.core.logging import get_logger
from trove.services.memory.models import MemoryScope

logger = get_logger(__name__)

PREFS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS preference_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    datasource TEXT NOT NULL,
    fact TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending|confirmed|rejected
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

HIGH_CONFIDENCE = 0.9
EXTRACT_LIMIT = 5

# 偏好提取候选的实义白名单:只收陈述式偏好/口径,拒绝对话噪音。
# "help me"/"is there"/"ok"/"thanks" 这类句式不是可复用记忆。
_NOISE_RE = re.compile(
    r"\b(help|thanks|thank you|ok|okay|sure|please|maybe|perhaps|"
    r"can you|could you|how about|what about|i think|i want)\b",
    re.IGNORECASE,
)
_MIN_FACT_LEN = 8


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_usable_preference(text: str) -> bool:
    """实义过滤:过短或含对话噪音句式的候选不写入(防记忆污染)。"""
    t = (text or "").strip()
    if len(t) < _MIN_FACT_LEN:
        return False
    if _NOISE_RE.search(t):
        return False
    return True


class PreferenceStore:
    """Pending-preference drafts (auto-extraction output awaiting confirm)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    async def _conn(self) -> aiosqlite.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self.db_path))
        await conn.execute(PREFS_TABLE_SQL)
        await conn.commit()
        return conn

    async def add(
        self, scope: MemoryScope, fact: str, *,
        evidence: str = "", confidence: float = 0.0,
    ) -> dict[str, Any]:
        """Insert one draft; equal-text drafts for the same scope refresh."""
        ts = now_iso()
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT id FROM preference_drafts "
                "WHERE user_id = ? AND datasource = ? AND fact = ?",
                (scope.user_id, scope.datasource, fact),
            )
            row = await cursor.fetchone()
            if row is not None:
                await conn.execute(
                    "UPDATE preference_drafts SET confidence = ?, evidence = ?, "
                    "status = 'pending', updated_at = ? WHERE id = ?",
                    (confidence, evidence[:200], ts, row[0]),
                )
                await conn.commit()
                return await self.get(row[0])
            await conn.execute(
                "INSERT INTO preference_drafts "
                "(user_id, datasource, fact, evidence, confidence, status, "
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (scope.user_id, scope.datasource, fact, evidence[:200],
                 confidence, ts, ts),
            )
            await conn.commit()
            return await self.get(cursor.lastrowid)
        finally:
            await conn.close()

    async def get(self, pref_id: int) -> dict[str, Any] | None:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT id, user_id, datasource, fact, evidence, confidence, "
                "status, created_at, updated_at FROM preference_drafts WHERE id = ?",
                (pref_id,),
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()
        return _row_to_dict(row) if row else None

    async def list_pending(self, scope: MemoryScope | None = None) -> list[dict[str, Any]]:
        conn = await self._conn()
        try:
            if scope is None or not scope.datasource:
                cursor = await conn.execute(
                    "SELECT id, user_id, datasource, fact, evidence, confidence, "
                    "status, created_at, updated_at FROM preference_drafts "
                    "WHERE status = 'pending' ORDER BY id",
                )
            else:
                cursor = await conn.execute(
                    "SELECT id, user_id, datasource, fact, evidence, confidence, "
                    "status, created_at, updated_at FROM preference_drafts "
                    "WHERE status = 'pending' AND user_id = ? AND datasource = ? "
                    "ORDER BY id",
                    (scope.user_id, scope.datasource),
                )
            rows = await cursor.fetchall()
        finally:
            await conn.close()
        return [_row_to_dict(r) for r in rows]

    async def set_status(self, pref_id: int, status: str) -> dict[str, Any] | None:
        conn = await self._conn()
        try:
            await conn.execute(
                "UPDATE preference_drafts SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso(), pref_id),
            )
            await conn.commit()
        finally:
            await conn.close()
        return await self.get(pref_id)

    async def purge(self, retention_days: int | None) -> int:
        if not retention_days:
            return 0
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "DELETE FROM preference_drafts WHERE status = 'rejected' "
                "AND updated_at < ?", (cutoff.isoformat(),),
            )
            await conn.commit()
            return cursor.rowcount
        finally:
            await conn.close()


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row[0], "user_id": row[1], "datasource": row[2], "fact": row[3],
        "evidence": row[4], "confidence": row[5], "status": row[6],
        "created_at": row[7], "updated_at": row[8],
    }


# ── LLM extraction (candidate preferences from a conversation) ──


def build_extract_prompt(conversation: str, lang: str = "zh") -> str:
    """最近对话 → 偏好候选提取提示词(结构化 JSON 输出)。"""
    from trove.prompts import render

    return render(
        "memory/preference_extract", lang=lang, conversation=conversation,
    )


def parse_extract_response(response: str) -> list[dict[str, Any]]:
    """LLM 回复 → 候选列表;非 JSON / 结构异常 → 空(静默降级)。"""
    text = (response or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    candidates = data.get("preferences") or data.get("candidates") or []
    if not isinstance(candidates, list):
        return []
    out: list[dict[str, Any]] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        fact = str(c.get("fact") or "").strip()
        if not fact:
            continue
        try:
            confidence = float(c.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        out.append({
            "fact": fact[:300],
            "confidence": max(0.0, min(1.0, confidence)),
            "evidence": str(c.get("evidence") or "")[:200],
        })
    return out[:EXTRACT_LIMIT]


async def extract_and_store(
    store: PreferenceStore,
    facts_service: Any | None,
    llm: Any,
    scope: MemoryScope,
    conversation: str,
    model: str,
    lang: str = "zh",
) -> dict[str, Any]:
    """LLM 提取偏好候选 → 高置信直入 user_facts / 低置信落 pending 草稿。

    Returns ``{"committed": [...], "drafted": [...], "skipped": [...]}``.
    Failures are silent — auto memory must never break the conversation.
    """
    if not scope.user_id or not scope.datasource or not conversation.strip():
        return {"committed": [], "drafted": [], "skipped": []}
    try:
        resp = await llm.chat(
            model=model,
            messages=[{"role": "user", "content": build_extract_prompt(conversation, lang)}],
        )
    except Exception as e:
        logger.warning("Preference extraction failed (%s): %s", scope.datasource, e)
        return {"committed": [], "drafted": [], "skipped": []}

    candidates = parse_extract_response(resp)
    committed, drafted, skipped = [], [], []
    for cand in candidates:
        fact = cand["fact"]
        if not is_usable_preference(fact):
            skipped.append({"fact": fact, "reason": "noise"})
            continue
        if cand["confidence"] >= HIGH_CONFIDENCE and facts_service is not None:
            try:
                await facts_service.add(scope.user_id, scope.datasource, fact)
                committed.append({"fact": fact, "confidence": cand["confidence"]})
                continue
            except Exception:
                pass  # write-policy rejection (too short/long/symbolic) → fall to draft
        try:
            await store.add(
                scope, fact, evidence=cand["evidence"], confidence=cand["confidence"],
            )
            drafted.append({"fact": fact, "confidence": cand["confidence"]})
        except Exception:
            logger.warning("Preference draft write failed: %s", fact)
    return {"committed": committed, "drafted": drafted, "skipped": skipped}
