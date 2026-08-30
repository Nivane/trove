"""Graph-state checkpoint persistence (LangGraph saver, SQLite or Postgres).

Dual-track persistence: conversation messages stay in SessionStore;
graph execution state lives in the checkpointer, keyed by thread_id =
session_id. The backend follows the unified storage resolution
(``TROVE_STORAGE_URL``):

- ``postgresql://...`` → ``AsyncPostgresSaver`` (production, langgraph-checkpoint-postgres);
- otherwise → ``AsyncSqliteSaver`` on ``{home}/checkpoints.db`` (tests/local, zero-network).

Usage:
    async with build_checkpointer(home_dir) as checkpointer:
        graphs = build_graphs(services, checkpointer=checkpointer)
        ...
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

# 生产 PG 时用 AsyncPostgresSaver;未配置(测试/本地)回落到 AsyncSqliteSaver。
_STORAGE_URL_ENV = "TROVE_STORAGE_URL"

CHECKPOINT_DB_NAME = "checkpoints.db"


def _use_postgres(storage_url: str | None) -> bool:
    return bool(storage_url) and any(
        storage_url.startswith(p) for p in ("postgresql://", "postgres://", "pg://")
    )


def build_checkpointer(
    home_dir: str | Path,
    storage_url: str | None = None,
) -> AsyncIterator[Any]:
    """Async context manager for a LangGraph checkpointer.

    Args:
        home_dir: Trove home directory (created if missing); used for the
            SQLite fallback path.
        storage_url: Explicit storage URL; when None, falls back to the
            ``TROVE_STORAGE_URL`` environment variable. A ``postgresql://``
            URL selects the Postgres saver; anything else (or empty) selects
            SQLite.

    Yields:
        A ready LangGraph checkpointer (setup/cleanup handled by the context).
    """
    url = storage_url if storage_url is not None else os.environ.get(_STORAGE_URL_ENV, "")
    if _use_postgres(url):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        return AsyncPostgresSaver.from_conn_string(url)

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    home = Path(home_dir).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver.from_conn_string(str(home / CHECKPOINT_DB_NAME))
