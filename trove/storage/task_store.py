"""Task persistence — cross-turn sub-task state.

Tasks share the unified SessionStore backend (single StorageBackend, tables
keyed by ``(project_name, session_id)``): messages/meta/tasks all live on one
backend, so deleting a session removes its tasks too (same-delete propagation
in ``SessionStore.delete_session``).

``SessionStore.compact_session`` rewrites messages in place (never deletes
the session row), so the tasks table survives compaction untouched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.core.types import Task

logger = get_logger(__name__)

TASKS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}',
    UNIQUE(project_name, session_id, task_id)
)
"""


class TaskStore:
    """Persistent storage for a session's task list (shares SessionStore backend)."""

    def __init__(self, backend, project_name: str, session_id: str):
        self._backend = backend
        self._project = project_name
        self._session_id = session_id
        self._schema_ready = False

    @classmethod
    def from_db_path(cls, db_path: str | Path, project_name: str, session_id: str) -> "TaskStore":
        """Compat constructor: derive a backend from a legacy db_path (tests)."""
        from trove.storage.backends import resolve_backend

        return cls(resolve_backend(str(db_path)), project_name, session_id)

    async def _conn(self):
        await self._ensure_schema()
        return self._backend

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        from trove.storage.backends.base import script_statements

        await self._backend.executescript(script_statements([TASKS_TABLE_SQL]))
        self._schema_ready = True

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
                "FROM tasks WHERE project_name = ? AND session_id = ? ORDER BY position",
                (self._project, self._session_id),
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
                "INSERT INTO tasks (project_name, session_id, task_id, title, status, "
                "position, created_at, updated_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project_name, session_id, task_id) DO UPDATE SET "
                "  title=excluded.title, status=excluded.status, position=excluded.position, "
                "  updated_at=excluded.updated_at, metadata_json=excluded.metadata_json",
                (
                    self._project, self._session_id, task.task_id, task.title,
                    task.status, task.position,
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
            await conn.execute(
                "DELETE FROM tasks WHERE project_name = ? AND session_id = ?",
                (self._project, self._session_id),
            )
            await conn.commit()
        finally:
            await conn.close()
