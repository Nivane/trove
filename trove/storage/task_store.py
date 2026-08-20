"""Task persistence — cross-turn sub-task state.

Tasks share the per-session SQLite file with SessionStore
(``~/.trove/sessions/{project}/{session_id}.db``): messages/meta/tasks are
three tables in one file, so the "one session = one .db" invariant holds
and deleting a session removes its tasks too.

``SessionStore.compact_session`` rewrites messages in place (never unlinks
the file), so the tasks table survives compaction untouched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from trove.core.logging import get_logger
from trove.core.types import Task

logger = get_logger(__name__)

TASKS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}'
)
"""


class TaskStore:
    """Persistent storage for a session's task list (same SQLite file as SessionStore)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    async def _conn(self) -> aiosqlite.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self.db_path))
        await conn.execute(TASKS_TABLE_SQL)
        await conn.commit()
        return conn

    @staticmethod
    def _row_to_task(row: tuple) -> Task:
        return Task(
            task_id=row[0],
            title=row[1],
            status=row[2],
            position=row[3],
            created_at=datetime.fromisoformat(row[4]),
            updated_at=datetime.fromisoformat(row[5]),
            metadata=json.loads(row[6]) if row[6] else {},
        )

    async def load_tasks(self) -> list[Task]:
        """All tasks of this session, ordered by position."""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT task_id, title, status, position, created_at, updated_at, metadata_json "
                "FROM tasks ORDER BY position"
            )
            tasks = [self._row_to_task(row) async for row in cursor]
        finally:
            await conn.close()
        return tasks

    async def save_task(self, task: Task) -> None:
        """Insert or update a task by task_id (position order is caller's concern)."""
        task.updated_at = datetime.now(timezone.utc)
        conn = await self._conn()
        try:
            await conn.execute(
                "INSERT INTO tasks (task_id, title, status, position, created_at, updated_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "  title=excluded.title, status=excluded.status, position=excluded.position, "
                "  updated_at=excluded.updated_at, metadata_json=excluded.metadata_json",
                (
                    task.task_id,
                    task.title,
                    task.status,
                    task.position,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                    json.dumps(task.metadata, ensure_ascii=False),
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def update_status(
        self,
        task_id: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> Task | None:
        """Mark a task's status (optionally merging metadata). Returns the updated task."""
        tasks = await self.load_tasks()
        for t in tasks:
            if t.task_id == task_id:
                if metadata:
                    t.metadata.update(metadata)
                t.status = status
                await self.save_task(t)
                return t
        logger.debug("update_status: task %s not found", task_id)
        return None

    async def clear(self) -> None:
        """Delete all tasks (/clear = fresh conversation, fresh tasks)."""
        conn = await self._conn()
        try:
            await conn.execute("DELETE FROM tasks")
            await conn.commit()
        finally:
            await conn.close()
