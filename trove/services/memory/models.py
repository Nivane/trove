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

# Re-exported: AgentConfig owns the config, so the class lives in core.
from trove.core.memory_config import MemoryConfig

__all__ = ["MemoryConfig", "MemoryEntry", "MemoryScope"]


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
    # 检索通道标记(观察用):episode 条目记录本命中实际使用的通道
    # ("lexical" 纯词面 / "hybrid" 词面+向量)。其余 kind 为空。
    channels: list[str] = field(default_factory=list)
