"""Memory data models — unified entry, scope, and configuration.

The memory subsystem unifies Trove's previously scattered memory layers
(KB / user facts / session / graph-state / agent-loop) behind one facade
(``MemoryService``) and one entry shape (``MemoryEntry``). New automatic
memory kinds (episodes, preferences) plug into the same model so the
context-budget assembly and admin surfaces see a single shape.

Persistence conventions follow the repo: aiosqlite open-per-operation,
idempotent CREATE TABLE IF NOT EXISTS, ISO-8601 text timestamps,
additive-only schema (see ``trove/services/user_facts/store.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemoryScope:
    """Where a memory item lives: datasource-scoped and/or user-scoped.

    KB entries are datasource-scoped (``user_id=""``); user facts,
    episodes and preferences are scoped to ``(user_id, datasource)``.
    """

    datasource: str = ""
    user_id: str = ""


@dataclass
class MemoryEntry:
    """One unified memory item returned by :meth:`MemoryService.retrieve`.

    ``score`` is the deterministic-gate relevance used by the context
    budget (item-level trim). ``idempotency_key`` dedups writes.
    """

    kind: str              # lesson|example|term|rule|fact|episode|preference
    scope: MemoryScope
    content: dict[str, Any]
    source: str = "auto"   # manual|auto|eval|user_rating|correction
    confidence: float = 0.0
    status: str = "pending"  # pending|confirmed|archived|merged
    score: float = 0.0
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str = ""
    hit_count: int = 0
    idempotency_key: str = ""


@dataclass
class MemoryConfig:
    """Memory subsystem configuration (``conf/agent.yml`` → ``agent.memory``).

    Progressive enablement: episodic + observations + auto-preferences on
    by default (they write *pending* content only and never block queries);
    auto-promotion and profile-injection are opt-in (safety gates).
    """

    enabled: bool = True
    episodes: bool = True            # cross-session episodic memory
    auto_examples: bool = True       # success → pending reference example
    auto_preferences: bool = True    # extract user calibers/preferences
    promotion: bool = False          # auto-confirm lessons/examples by score
    promotion_threshold: float = 0.8
    profile_boost: bool = False      # inject user×datasource failure profile
    schema_drift_check: bool = True
    # retention (days): None = keep forever
    retention_days: dict[str, int | None] = field(default_factory=dict)

    @property
    def episodes_enabled(self) -> bool:
        return self.enabled and self.episodes

    @property
    def examples_enabled(self) -> bool:
        return self.enabled and self.auto_examples

    @property
    def preferences_enabled(self) -> bool:
        return self.enabled and self.auto_preferences

    @property
    def promotion_enabled(self) -> bool:
        return self.enabled and self.promotion

    @property
    def profile_enabled(self) -> bool:
        return self.enabled and self.profile_boost
