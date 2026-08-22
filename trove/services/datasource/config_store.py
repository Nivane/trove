"""Datasource metadata persistence — .trove/datasources.yml.

YAML is the single source of truth (same philosophy as the KB files);
the registry itself stays in-memory and is rebuilt from this file on
boot. Credentials live only in this local, gitignored file — every
list API sanitizes them away (see registry._sanitize_connection).
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from trove.core.errors import DatasourceError
from trove.core.types import DatasourceConfig

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(".trove") / "datasources.yml"


def to_dict(cfg: DatasourceConfig) -> dict:
    return {
        "name": cfg.name,
        "type": cfg.type,
        "connection": dict(cfg.connection_params),
        "credentials": dict(cfg.credentials),
        "default": bool(cfg.default),
    }


def from_dict(data: dict) -> DatasourceConfig:
    return DatasourceConfig(
        name=data["name"],
        type=data["type"],
        connection_params=dict(data.get("connection", {})),
        credentials=dict(data.get("credentials", {})),
        default=bool(data.get("default", False)),
    )


class ConfigStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else DEFAULT_PATH

    def load_configs(self) -> list[DatasourceConfig]:
        if not self.path.exists():
            return []
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise DatasourceError(
                message=f"corrupt datasources.yml: {e}", datasource="",
            ) from e
        return [from_dict(d) for d in data.get("datasources", [])]

    def save_configs(self, configs: list[DatasourceConfig]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            yaml.safe_dump({"datasources": [to_dict(c) for c in configs]},
                           allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


async def boot_register(registry, configs: list[DatasourceConfig]) -> list[str]:
    """Register persisted datasources at boot; connection failures are
    logged and skipped (the admin UI shows them as disconnected)."""
    failed: list[str] = []
    for cfg in configs:
        try:
            if cfg.type == "demo":
                from trove.services.datasource.demo_setup import (
                    setup_demo_datasource,
                )
                await setup_demo_datasource(registry)
            else:
                await registry.register(cfg, set_default=cfg.default)
        except (DatasourceError, OSError) as e:
            failed.append(cfg.name)
            logger.warning("boot register '%s' skipped: %s", cfg.name, e)
    return failed
