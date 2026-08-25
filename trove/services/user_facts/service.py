"""User facts service — policy + retrieval scoring over :class:`UserFactsStore`.

A user fact is a short, self-service statement of the user's preference or
business caliber for a datasource (e.g. "营收 = 净收入", "看日均用 30 日均值").
Unlike the datasource-level KB, facts are owned by a single user and scoped
to ``(user_id, datasource)``, giving each user personalization on top of the
shared semantic model.

Facts are injected into SQL generation (gen_sql context block) after being
scored against the question with the same lexical overlap used for lessons
(``relevance_score``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.services.user_facts.store import UserFactsStore
from trove.workflow.context_score import relevance_score

logger = get_logger(__name__)

SEARCH_LIMIT = 3  # facts injected into the gen_sql prompt per run


class UserFactsService:
    """CRUD + question-relevance retrieval of per-user facts."""

    def __init__(self, db_path: str | Path):
        self.store = UserFactsStore(db_path)

    async def add(self, user_id: str, datasource: str, fact: str) -> dict[str, Any]:
        """Add a fact for (user, datasource); returns the stored row."""
        fact = (fact or "").strip()
        if not fact:
            raise ValueError("fact must not be empty")
        return await self.store.add(user_id, datasource, fact)

    async def list(
        self, user_id: str, datasource: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.store.list(user_id, datasource)

    async def get(self, user_id: str, fact_id: int) -> dict[str, Any] | None:
        return await self.store.get(user_id, fact_id)

    async def update(
        self, user_id: str, fact_id: int, *, fact: str | None = None,
        datasource: str | None = None,
    ) -> dict[str, Any] | None:
        """Update a fact's text/datasource; None when not owned."""
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

        Relevance drives ordering so the highest-signal facts fill the prompt
        first; every fact of the user+datasource is a retrieval candidate (a
        stated fact is a hard preference once told).
        """
        facts = await self.store.list(user_id, datasource)
        scored = sorted(
            facts,
            key=lambda f: relevance_score(str(f["fact"]), question),
            reverse=True,
        )
        return scored[: max(limit, 0)]

    # ── Admin ─────────────────────────────────────────────

    async def list_all(
        self, user_id: str | None = None, datasource: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.store.list_all(user_id=user_id, datasource=datasource)

    async def delete_any(self, fact_id: int) -> bool:
        return await self.store.delete_any(fact_id)
