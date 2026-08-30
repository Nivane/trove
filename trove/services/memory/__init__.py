"""Memory subsystem — unified facade over Trove's memory layers.

Provides:
  - ``MemoryService``   facade: observe/store/retrieve/lifecycle/profile
  - ``MemoryConfig``    progressive-enablement configuration
  - episodic memory     (cross-session "what happened" recall)
  - auto-preferences    (LLM extraction → draft → confirm)
  - auto-promotion      (confidence-based lesson/example confirmation)
  - schema-drift check  (live schema vs KB memory)
  - user×datasource profile (correctness / failure patterns)
"""

from __future__ import annotations

from trove.services.memory.episode import EpisodeStore
from trove.services.memory.models import MemoryConfig, MemoryEntry, MemoryScope
from trove.services.memory.service import MemoryService

__all__ = [
    "MemoryService",
    "MemoryConfig",
    "MemoryEntry",
    "MemoryScope",
    "EpisodeStore",
]
