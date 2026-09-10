"""Storage layer for Trove."""

from __future__ import annotations

from typing import Any

__all__ = ["SessionStore", "ConfigStore"]


def __getattr__(name: str) -> Any:
    """惰性再导出。

    顶层再导出会成环:``ConfigStore`` → ``core.config`` → ``services.memory``
    → 存储层。存储层必须是**谁都能先导入**的那一个(``storage.migrations``
    被记忆、检索、KB 镜像三处引用),所以包级导入保持零副作用。
    """
    if name == "SessionStore":
        from trove.storage.session_store import SessionStore

        return SessionStore
    if name == "ConfigStore":
        from trove.storage.config_store import ConfigStore

        return ConfigStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
