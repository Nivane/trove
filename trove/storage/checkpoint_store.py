"""Graph-state checkpoint persistence (LangGraph AsyncSqliteSaver).

Dual-track persistence: conversation messages stay in SessionStore;
graph execution state lives in the checkpointer DB ({home}/checkpoints.db),
keyed by thread_id = session_id.

Usage:
    async with build_checkpointer(home_dir) as checkpointer:
        graphs = build_graphs(services, checkpointer=checkpointer)
        ...
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

CHECKPOINT_DB_NAME = "checkpoints.db"


def build_checkpointer(home_dir: str | Path) -> AsyncIterator[AsyncSqliteSaver]:
    """Async context manager for an AsyncSqliteSaver backed by {home_dir}/checkpoints.db.

    Args:
        home_dir: Trove home directory (created if missing).

    Yields:
        A ready AsyncSqliteSaver (setup/cleanup handled by the context).
    """
    home = Path(home_dir).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver.from_conn_string(str(home / CHECKPOINT_DB_NAME))
