"""User facts service — policy + retrieval scoring over :class:`UserFactsStore`.

A user fact is a short, self-service statement of the user's preference or
business caliber for a datasource (e.g. "营收 = 净收入", "看日均用 30 日均值").
Unlike the datasource-level KB, facts are owned by a single user and scoped
to ``(user_id, datasource)``, giving each user personalization on top of the
shared semantic model.

Facts are injected into SQL generation (gen_sql context block) after being
scored against the question with the same lexical overlap used for lessons
(``relevance_score``).

记忆写入策略(对应记忆深度版"写入什么 / 什么时候遗忘"):
- 写入:只收"短、自包含、有实义"的陈述式事实;等值事实幂等刷新
  (冲突消解,不重复堆积 → 防记忆污染)。
- 遗忘:检索时按 ``updated_at`` 半衰衰减、超期不注入(生命周期);物理
  清理由 ``purge_expired`` 显式触发。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.services.user_facts.store import UserFactsStore
from trove.workflow.context_score import relevance_score

logger = get_logger(__name__)

SEARCH_LIMIT = 3  # facts injected into the gen_sql prompt per run

MIN_FACT_LEN = 2  # 少于 2 个实义字符 = 噪音(如 "x"),单字符无信息量
MAX_FACT_LEN = 300  # 过长 = 非"短陈述",应总结为一条事实
CONTENT_RE = re.compile(r"[A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff]")

FACT_DECAY_HALF_LIFE_DAYS = 90  # 个人化事实半衰比教训更短
FACT_EXPIRY_DAYS = 180  # 超期未触碰的事实不再注入(遗忘),仍保留可查


def normalize_fact(text: str) -> str:
    """规范化:去首尾空白 + 压缩内部连续空白(事实是单行短陈述)。"""
    return re.sub(r"\s+", " ", (text or "").strip())


def validate_fact(text: str) -> str:
    """写入策略校验:返回规范化文本,不合法 raise ValueError(带原因)。

    存什么不存什么:只收短、自包含、含实义字符的陈述;空、纯符号、
    过长文本拒绝(提示用户自行总结)。
    """
    fact = normalize_fact(text)
    if not fact:
        raise ValueError("fact must not be empty")
    if len(fact) < MIN_FACT_LEN:
        raise ValueError(f"fact too short (< {MIN_FACT_LEN} chars): {fact!r}")
    if len(fact) > MAX_FACT_LEN:
        raise ValueError(
            f"fact too long (> {MAX_FACT_LEN} chars) — summarize it into one "
            f"short statement: {fact[:40]}…"
        )
    if not CONTENT_RE.search(fact):
        raise ValueError(f"fact has no content (only symbols/punctuation): {fact!r}")
    return fact


def _recency_factor(ts: str | None, half_life_days: int = FACT_DECAY_HALF_LIFE_DAYS) -> float:
    """时效衰减因子:0.4~1.0,半衰期后折半——久远事实排序下降但不抹掉。"""
    if not ts:
        return 1.0
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    except Exception:
        return 1.0
    return 0.4 + 0.6 * (0.5 ** (days / half_life_days))


def _is_expired(ts: str | None, expiry_days: int = FACT_EXPIRY_DAYS) -> bool:
    """是否已"遗忘":超过 expiry_days 未写入/更新。"""
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400 > expiry_days
    except Exception:
        return False


class UserFactsService:
    """CRUD + question-relevance retrieval of per-user facts."""

    def __init__(self, db_path: str | Path):
        self.store = UserFactsStore(db_path)

    async def dispose(self) -> None:
        """Release the store's backend connection (see UserFactsStore.dispose)."""
        await self.store.dispose()

    async def add(self, user_id: str, datasource: str, fact: str) -> dict[str, Any]:
        """Add a fact for (user, datasource); returns the stored row.

        写入策略:校验通过后做冲突消解——规范化等值事实已存在则幂等刷新
        (更新 updated_at,保持同一条 id),防止重复事实堆积污染记忆。
        """
        fact = validate_fact(fact)
        existing = await self.store.find_by_text(user_id, datasource, fact)
        if existing is not None:
            return await self.store.update(user_id, existing["id"], fact=fact) or existing
        return await self.store.add(user_id, datasource, fact)

    async def list(
        self, user_id: str, datasource: str | None = None,
    ) -> list[dict[str, Any]]:
        """原始 CRUD 视图:用户自己的全部事实(含已过期,管理端可见)。"""
        return await self.store.list(user_id, datasource)

    async def get(self, user_id: str, fact_id: int) -> dict[str, Any] | None:
        return await self.store.get(user_id, fact_id)

    async def update(
        self, user_id: str, fact_id: int, *, fact: str | None = None,
        datasource: str | None = None,
    ) -> dict[str, Any] | None:
        """Update a fact's text/datasource; None when not owned.

        先校验归属(非本人 → None → 404)再校验内容,避免越权请求在
        内容校验上先报错;更新文本同样走写入校验(规范化 + 长度/实义约束)。
        """
        if await self.store.get(user_id, fact_id) is None:
            return None
        if fact is not None:
            fact = validate_fact(fact)
        return await self.store.update(
            user_id, fact_id, fact=fact, datasource=datasource,
        )

    async def delete(self, user_id: str, fact_id: int) -> bool:
        return await self.store.delete(user_id, fact_id)

    async def search(
        self, user_id: str, datasource: str, question: str,
        limit: int = SEARCH_LIMIT,
    ) -> list[dict[str, Any]]:
        """Facts of (user, datasource) scored by relevance to the question, top-N.

        遗忘策略:超期(未触碰超过 FACT_EXPIRY_DAYS)的事实不注入;剩余候选
        按 ``relevance * recency`` 排序——相关度为主,最近度做温和微调,
        久远但高度相关的事实仍能进上下文。
        """
        facts = await self.store.list(user_id, datasource)
        candidates = [f for f in facts if not _is_expired(f.get("updated_at"))]
        scored = sorted(
            candidates,
            key=lambda f: relevance_score(str(f["fact"]), question)
            * _recency_factor(str(f.get("updated_at"))),
            reverse=True,
        )
        return scored[: max(limit, 0)]

    async def purge_expired(
        self, days: int = FACT_EXPIRY_DAYS,
    ) -> int:
        """物理清理超期事实(记忆压缩);返回删除条数。"""
        return await self.store.purge_expired(days)

    # ── Admin ─────────────────────────────────────────────

    async def list_all(
        self, user_id: str | None = None, datasource: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.store.list_all(user_id=user_id, datasource=datasource)

    async def delete_any(self, fact_id: int) -> bool:
        return await self.store.delete_any(fact_id)
