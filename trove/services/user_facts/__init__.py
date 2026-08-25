"""User-level memory layer (Mem0-style) — per-user facts, datasource-scoped.

Independent of the datasource-level KB: facts are owned by a single user
and carry that user's preferences / business calibers (e.g. "营收 = 净收入",
"看日均用 30 日均值"), giving each user personalization on top of the shared
semantic model.
"""

from trove.services.user_facts.service import UserFactsService
from trove.services.user_facts.store import UserFactsStore

__all__ = ["UserFactsService", "UserFactsStore"]
