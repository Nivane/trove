"""Project-level configuration store.

Reads and writes .trove/config.yml with whitelist enforcement.
Only whitelisted keys can be persisted; anything else is silently
ignored on write and filtered on read.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from trove.core.config import PROJECT_CONFIG_WHITELIST, ProjectConfig
from trove.core.logging import get_logger

logger = get_logger(__name__)

CONFIG_FILE_NAME = "config.yml"
TROVE_DIR_NAME = ".trove"


class ConfigStore:
    """Manages persistent project-level configuration."""

    def __init__(self, project_root: str | Path = "."):
        self.project_root = Path(project_root).resolve()
        self.config_dir = self.project_root / TROVE_DIR_NAME
        self.config_path = self.config_dir / CONFIG_FILE_NAME

    def exists(self) -> bool:
        """Check if .trove/config.yml exists."""
        return self.config_path.exists()

    def load(self) -> ProjectConfig:
        """Load whitelisted config keys from .trove/config.yml.

        Non-whitelisted keys in the file are silently ignored.

        Returns:
            ProjectConfig with loaded values.
        """
        if not self.exists():
            return ProjectConfig()

        try:
            raw_text = self.config_path.read_text(encoding="utf-8")
            raw = yaml.safe_load(raw_text) or {}
        except (yaml.YAMLError, OSError) as e:
            logger.warning("Cannot read %s: %s", self.config_path, e)
            return ProjectConfig()

        if not isinstance(raw, dict):
            return ProjectConfig()

        filtered = {k: v for k, v in raw.items() if k in PROJECT_CONFIG_WHITELIST}

        return ProjectConfig(
            target=str(filtered.get("target", "")),
            default_datasource=str(filtered.get("default_datasource", "")),
            project_name=str(filtered.get("project_name", "")),
            scheduler=str(filtered.get("scheduler", "")),
        )

    def save(self, config: ProjectConfig) -> None:
        """Save project config, writing only whitelisted keys.

        Args:
            config: The project configuration to persist.
        """
        self.config_dir.mkdir(parents=True, exist_ok=True)

        data = {}
        if config.target:
            data["target"] = config.target
        if config.default_datasource:
            data["default_datasource"] = config.default_datasource
        if config.project_name:
            data["project_name"] = config.project_name
        if config.scheduler:
            data["scheduler"] = config.scheduler

        # Verify all keys are whitelisted
        for key in data:
            if key not in PROJECT_CONFIG_WHITELIST:
                logger.warning(
                    "Key '%s' is not whitelisted for project config; refusing to write",
                    key,
                )
                return

        yaml_text = yaml.safe_dump(data, default_flow_style=False, allow_unicode=True)
        self.config_path.write_text(yaml_text, encoding="utf-8")
        logger.debug("Saved project config to %s", self.config_path)

    def update(self, **kwargs: str) -> ProjectConfig:
        """Update one or more whitelisted keys.

        Only keys in PROJECT_CONFIG_WHITELIST are accepted.

        Args:
            **kwargs: Key-value pairs to update.

        Returns:
            Updated ProjectConfig.
        """
        current = self.load()

        for key, value in kwargs.items():
            if key not in PROJECT_CONFIG_WHITELIST:
                logger.warning("Key '%s' is not whitelisted; skipping", key)
                continue
            setattr(current, key, value)

        self.save(current)
        return current
