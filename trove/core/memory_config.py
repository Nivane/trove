"""Memory subsystem configuration (``conf/agent.yml`` → ``agent.memory``).

Lives in ``core`` because :class:`trove.core.config.AgentConfig` owns it;
``trove.services.memory.models`` re-exports it so the memory package's public
import path is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
