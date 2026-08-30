"""Session persistence — single backend, multi-table (StorageBackend).

Sessions previously lived in per-session SQLite files; to unify Trove's
internal state on one StorageBackend (PostgreSQL in production, in-memory
SQLite in tests/local) the model becomes one database with session-scoped
rows keyed by ``(project_name, session_id)``:

  sessions — one row per conversation (meta incl. summary/branch)
  messages — per-session messages
  meta     — per-session key/value (compat with the old file-per-session)
  tasks    — per-session sub-tasks (TaskStore shares this backend)

The public API is unchanged (create/load/save/set_title/delete/list_all/
clear/compact); callers do not see the storage layout.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trove.core.types import Message, Session
from trove.core.errors import SessionError
from trove.core.logging import get_logger
from trove.storage.task_store import TASKS_TABLE_SQL

logger = get_logger(__name__)

# ── Schema (portable across SQLite / PostgreSQL) ─────────

CREATE_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    project_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'local',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    summary TEXT,
    branch_parent TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (project_name, session_id)
)
"""

CREATE_MESSAGES_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}'
)
"""

CREATE_META_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    project_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (project_name, session_id, key)
)
"""

# 兼容字段命名:旧 per-session 文件的 messages 表没有 project/session 列,
# 这里显式列序对齐存储后端(INSERT 显式列名,不受影响)。
_SESSIONS_SCRIPT = [
    CREATE_SESSIONS_SQL,
    CREATE_MESSAGES_SQL,
    CREATE_META_SQL,
    TASKS_TABLE_SQL,
]

# ── Helpers ──────────────────────────────────────────────


def _normalize_project_name(cwd: str | Path) -> str:
    """Normalize a directory path into a safe project name.

    Long paths get a short prefix + md5 suffix to keep
    filenames manageable while avoiding collisions.

    Examples:
        /home/user/my_project → my_project
        /very/long/path/.../deep → deep_a1b2c3d4
    """
    name = str(cwd).rstrip("/").split("/")[-1] or "default"
    safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
    if len(safe_name) > 40:
        import hashlib
        h = hashlib.md5(str(cwd).encode()).hexdigest()[:8]
        safe_name = f"{safe_name[:30]}_{h}"
    return safe_name or "default"


# ── SessionStore ─────────────────────────────────────────


class SessionStore:
    """Conversation sessions persisted on one StorageBackend (multi-table)."""

    def __init__(self, home_dir: str | Path = "~/.trove"):
        self.home_dir = Path(home_dir).expanduser().resolve()
        # 统一后端:生产 PG(TROVE_STORAGE_URL)/ 测试本地 SQLite(内存或文件)。
        from trove.storage.backends import resolve_backend

        self._backend = resolve_backend(str(self.home_dir / "sessions.sqlite"))
        self._schema_ready = False

    # ── Backend / schema ─────────────────────────────────

    def backend(self):
        """Shared StorageBackend (messages/meta/tasks 同库,TaskStore 复用)。"""
        return self._backend

    async def _conn(self):
        await self._ensure_schema()
        return self._backend

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        from trove.storage.backends.base import script_statements

        await self._backend.executescript(script_statements(_SESSIONS_SCRIPT))
        self._schema_ready = True

    async def dispose(self) -> None:
        """释放后端连接(进程退出/显式清理)。"""
        await self._backend.dispose()

    # ── CRUD: Create ─────────────────────────────────────

    async def create_session(
        self,
        project_cwd: str | Path = ".",
        user_id: str = "local",
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        """Create a new session and persist it."""
        project_name = _normalize_project_name(project_cwd)
        session = Session(
            session_id=str(uuid.uuid4()),
            project_name=project_name,
            user_id=user_id,
            metadata=metadata or {},
        )
        conn = await self._conn()
        try:
            await conn.execute(
                "INSERT INTO sessions (project_name, session_id, user_id, created_at, "
                "updated_at, summary, branch_parent, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_name, session.session_id, user_id,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.summary or "", session.branch_parent,
                    json.dumps(session.metadata, ensure_ascii=False),
                ),
            )
            # 兼容旧 per-session 文件的 meta 表:持久化关键键
            await self._upsert_meta(conn, project_name, session.session_id, {
                "project_name": project_name,
                "user_id": user_id,
                "created_at": session.created_at.isoformat(),
                "summary": session.summary or "",
                "updated_at": session.updated_at.isoformat(),
            })
            await conn.commit()
        finally:
            await conn.close()
        logger.debug("Created session %s in project %s", session.session_id, project_name)
        return session

    @staticmethod
    async def _upsert_meta(conn, project: str, session_id: str, kv: dict[str, Any]) -> None:
        for key, value in kv.items():
            await conn.execute(
                "INSERT INTO meta (project_name, session_id, key, value) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_name, session_id, key) DO UPDATE SET value = excluded.value",
                (project, session_id, key, "" if value is None else str(value)),
            )

    # ── CRUD: Read ───────────────────────────────────────

    async def load_session(
        self,
        session_id: str,
        project_cwd: str | Path = ".",
    ) -> Session:
        """Load an existing session from storage."""
        project_name = _normalize_project_name(project_cwd)
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT project_name, session_id, user_id, created_at, updated_at, "
                "summary, branch_parent, metadata_json "
                "FROM sessions WHERE project_name = ? AND session_id = ?",
                (project_name, session_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise SessionError(
                    message=f"Session {session_id} not found",
                    session_id=session_id,
                    details={"project": project_name},
                )
            # 兼容:从 meta 取回(保留旧字段语义)
            meta = {}
            mcur = await conn.execute(
                "SELECT key, value FROM meta WHERE project_name = ? AND session_id = ?",
                (project_name, session_id),
            )
            async for mrow in mcur:
                meta[mrow[0]] = mrow[1]
            messages = []
            ccur = await conn.execute(
                "SELECT role, content, timestamp, metadata_json FROM messages "
                "WHERE project_name = ? AND session_id = ? ORDER BY id",
                (project_name, session_id),
            )
            async for mrow in ccur:
                messages.append(Message(
                    role=mrow[0],
                    content=mrow[1],
                    timestamp=datetime.fromisoformat(mrow[2]),
                    metadata=json.loads(mrow[3]) if mrow[3] else {},
                ))
        finally:
            await conn.close()

        return Session(
            session_id=session_id,
            project_name=row[0],
            user_id=row[2] or meta.get("user_id", "local"),
            messages=messages,
            summary=(row[5] or meta.get("summary") or None),
            branch_parent=row[6] or meta.get("branch_parent"),
            created_at=datetime.fromisoformat(row[3])
                if row[3] else datetime.now(timezone.utc),
            updated_at=datetime.fromisoformat(row[4])
                if row[4] else datetime.now(timezone.utc),
            metadata=json.loads(row[7]) if row[7] else {},
        )

    # ── CRUD: Update ─────────────────────────────────────

    async def save_session(self, session: Session) -> None:
        """Persist a session's messages and metadata (append-only messages)."""
        conn = await self._conn()
        try:
            # 现有消息数(同 session 内计数,按 project+session 过滤)
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM messages WHERE project_name = ? AND session_id = ?",
                (session.project_name, session.session_id),
            )
            row = await cursor.fetchone()
            existing_count = row[0] if row else 0

            new_messages = session.messages[existing_count:]
            for msg in new_messages:
                await conn.execute(
                    "INSERT INTO messages (project_name, session_id, role, content, "
                    "timestamp, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        session.project_name, session.session_id, msg.role, msg.content,
                        msg.timestamp.isoformat(),
                        json.dumps(msg.metadata, ensure_ascii=False),
                    ),
                )
            session.updated_at = datetime.now(timezone.utc)
            await self._upsert_meta(conn, session.project_name, session.session_id, {
                "summary": session.summary or "",
                "updated_at": session.updated_at.isoformat(),
                "project_name": session.project_name,
                "user_id": session.user_id,
            })
            await conn.execute(
                "UPDATE sessions SET updated_at = ?, summary = ? "
                "WHERE project_name = ? AND session_id = ?",
                (
                    session.updated_at.isoformat(), session.summary or "",
                    session.project_name, session.session_id,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def set_title(
        self,
        session_id: str,
        title: str,
        project_cwd: str | Path = ".",
    ) -> bool:
        """Rename a session (persisted in the meta table)."""
        project_name = _normalize_project_name(project_cwd)
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT 1 FROM sessions WHERE project_name = ? AND session_id = ?",
                (project_name, session_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return False
            await self._upsert_meta(conn, project_name, session_id, {
                "title": title or "",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            await conn.commit()
            return True
        finally:
            await conn.close()

    # ── CRUD: Delete ─────────────────────────────────────

    async def delete_session(
        self,
        session_id: str,
        project_cwd: str | Path = ".",
    ) -> bool:
        """Delete a session (and its messages/meta) from storage."""
        project_name = _normalize_project_name(project_cwd)
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT 1 FROM sessions WHERE project_name = ? AND session_id = ?",
                (project_name, session_id),
            )
            if await cursor.fetchone() is None:
                return False
            for table in ("tasks", "messages", "meta"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE project_name = ? AND session_id = ?",
                    (project_name, session_id),
                )
            await conn.execute(
                "DELETE FROM sessions WHERE project_name = ? AND session_id = ?",
                (project_name, session_id),
            )
            await conn.commit()
            logger.debug("Deleted session %s", session_id)
            return True
        finally:
            await conn.close()

    # ── CRUD: List ───────────────────────────────────────

    async def list_sessions(
        self,
        project_cwd: str | Path = ".",
        user_id: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """List sessions for a project (by updated_at, desc)."""
        project_name = _normalize_project_name(project_cwd)
        conn = await self._conn()
        try:
            where, params = ["project_name = ?"], [project_name]
            if user_id is not None:
                where.append("user_id = ?")
                params.append(user_id)
            cursor = await conn.execute(
                f"SELECT project_name, session_id, user_id, created_at, updated_at "
                f"FROM sessions WHERE {' AND '.join(where)} ORDER BY updated_at DESC",
                tuple(params),
            )
            rows = await cursor.fetchall()
        finally:
            await conn.close()
        results = []
        for row in rows:
            info = await self._session_info(project_name, row[1], row[2], row[3], row[4])
            if info:
                results.append(info)
        if offset:
            results = results[offset:]
        if limit is not None:
            results = results[:limit]
        return results

    async def _session_info(
        self, project_name: str, session_id: str, user_id: str,
        created_at: str, updated_at: str,
    ) -> dict[str, Any] | None:
        """Aggregate one session's list-row metadata."""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM messages WHERE project_name = ? AND session_id = ?",
                (project_name, session_id),
            )
            row = await cursor.fetchone()
            msg_count = row[0] if row else 0
            cursor = await conn.execute(
                "SELECT content FROM messages WHERE project_name = ? AND session_id = ? "
                "AND role = 'user' ORDER BY id LIMIT 1",
                (project_name, session_id),
            )
            row = await cursor.fetchone()
            first_question = row[0] if row else ""
            cursor = await conn.execute(
                "SELECT value FROM meta WHERE project_name = ? AND session_id = ? AND key = 'title'",
                (project_name, session_id),
            )
            row = await cursor.fetchone()
            custom_title = (row[0] if row else "") or ""
        finally:
            await conn.close()
        return {
            "session_id": session_id,
            "project_name": project_name,
            "user_id": user_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "message_count": msg_count,
            "title": custom_title or first_question,
            "size_bytes": 0,  # 单库多表模型下无独立文件体积
        }

    async def list_all(
        self,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List sessions across ALL projects (maintenance sweep)."""
        conn = await self._conn()
        try:
            if user_id is not None:
                cursor = await conn.execute(
                    "SELECT project_name, session_id, user_id, created_at, updated_at "
                    "FROM sessions WHERE user_id = ? ORDER BY updated_at DESC",
                    (user_id,),
                )
            else:
                cursor = await conn.execute(
                    "SELECT project_name, session_id, user_id, created_at, updated_at "
                    "FROM sessions ORDER BY updated_at DESC",
                )
            rows = await cursor.fetchall()
        finally:
            await conn.close()
        results = []
        for row in rows:
            info = await self._session_info(row[0], row[1], row[2], row[3], row[4])
            if info:
                results.append(info)
        return results

    # ── Session operations ───────────────────────────────

    async def set_updated_at(
        self, project_name: str, session_id: str, updated_at: datetime,
    ) -> None:
        """测试/维护钩子:改写会话 updated_at(维护老化模拟用)。"""
        conn = await self._conn()
        try:
            await self._upsert_meta(conn, project_name, session_id, {
                "updated_at": updated_at.isoformat(),
            })
            await conn.execute(
                "UPDATE sessions SET updated_at = ? "
                "WHERE project_name = ? AND session_id = ?",
                (updated_at.isoformat(), project_name, session_id),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def clear_session(self, session: Session) -> Session:
        """Remove all messages and the compaction summary (keep the session)."""
        conn = await self._conn()
        try:
            await conn.execute(
                "DELETE FROM messages WHERE project_name = ? AND session_id = ?",
                (session.project_name, session.session_id),
            )
            await self._upsert_meta(conn, session.project_name, session.session_id, {
                "summary": "",
            })
            await conn.execute(
                "UPDATE sessions SET updated_at = ?, summary = NULL "
                "WHERE project_name = ? AND session_id = ?",
                (
                    datetime.now(timezone.utc).isoformat(),
                    session.project_name, session.session_id,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()
        session.messages = []
        session.summary = None
        session.updated_at = datetime.now(timezone.utc)
        logger.debug("Cleared session %s", session.session_id)
        return session

    async def compact_session(
        self,
        session: Session,
        summary_text: str,
        keep_recent: int = 3,
    ) -> Session:
        """Compact a session by replacing old messages with a summary."""
        keep_count = min(keep_recent * 2, len(session.messages))
        recent = session.messages[-keep_count:] if keep_count > 0 else []

        summary_msg = Message(
            role="system",
            content=f"[Conversation Summary]\n{summary_text}",
            metadata={"type": "compaction_summary"},
        )
        session.messages = [summary_msg] + recent
        session.summary = summary_text

        conn = await self._conn()
        try:
            await conn.execute(
                "DELETE FROM messages WHERE project_name = ? AND session_id = ?",
                (session.project_name, session.session_id),
            )
            for msg in session.messages:
                await conn.execute(
                    "INSERT INTO messages (project_name, session_id, role, content, "
                    "timestamp, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        session.project_name, session.session_id, msg.role, msg.content,
                        msg.timestamp.isoformat(),
                        json.dumps(msg.metadata, ensure_ascii=False),
                    ),
                )
            await self._upsert_meta(conn, session.project_name, session.session_id, {
                "summary": summary_text,
                "project_name": session.project_name,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            await conn.execute(
                "UPDATE sessions SET updated_at = ?, summary = ? "
                "WHERE project_name = ? AND session_id = ?",
                (
                    datetime.now(timezone.utc).isoformat(), summary_text,
                    session.project_name, session.session_id,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()
        logger.debug("Compacted session %s", session.session_id)
        return session
