"""Datasource metadata persistence — .trove/datasources.yml.

YAML is the single source of truth (same philosophy as the KB files);
the registry itself stays in-memory and is rebuilt from this file on
boot. Credentials live only in this local, gitignored file — every
list API sanitizes them away (see registry._sanitize_connection).
"""

from __future__ import annotations

import logging
import os
import tempfile
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
        # 合法 YAML 但错误形状(手改/损坏):顶层非映射或 datasources 非列表
        # 都会在迭代时抛裸 AttributeError/TypeError → 启动裸 traceback。统一
        # 包装为 DatasourceError,与 KeyError/YAMLError 同一失败契约(fail-fast)。
        if not isinstance(data, dict):
            raise DatasourceError(
                message="corrupt datasources.yml: top-level must be a mapping",
                datasource="",
            )
        if not isinstance(data.get("datasources"), list):
            raise DatasourceError(
                message="corrupt datasources.yml: 'datasources' must be a list",
                datasource="",
            )
        configs = []
        for d in data.get("datasources", []):
            try:
                configs.append(from_dict(d))
            except KeyError as e:
                name = d.get("name", "<unknown>") if isinstance(d, dict) else "<unknown>"
                raise DatasourceError(
                    message=f"corrupt datasources.yml entry for '{name}': missing {e}",
                    datasource=name,
                ) from e
        return configs

    def save_configs(self, configs: list[DatasourceConfig]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(
            {"datasources": [to_dict(c) for c in configs]},
            allow_unicode=True, sort_keys=False,
        )
        # 原子写:先写同目录临时文件再 os.replace,避免写中断留下截断的
        # datasources.yml 把下次启动锁死(管理端可写文件自我破坏路径)。
        fd, tmp_path = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}-", suffix=".tmp",
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


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
                # set_default 必须与持久化的 cfg.default 一致，否则非默认 demo
                # 重启后会被错误恢复为默认（admin 注册时已按当时默认态持久化）
                await setup_demo_datasource(registry, set_default=cfg.default)
            else:
                await registry.register(cfg, set_default=cfg.default)
        except (DatasourceError, OSError) as e:
            failed.append(cfg.name)
            logger.warning("boot register '%s' skipped: %s", cfg.name, e)
    return failed
