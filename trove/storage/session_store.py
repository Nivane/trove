"""Session persistence using SQLite.

Sessions are stored per-project:
  ~/.trove/sessions/{project_name}/{session_id}.db

Each session is a SQLite database with a 'messages' table
and a 'meta' table for session-level data.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from trove.core.types import Message, Session
from trove.core.errors import SessionError
from trove.core.logging import get_logger

logger = get_logger(__name__)

# ── Schema ───────────────────────────────────────────────

MESSAGES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}'
)
"""

META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


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
    # Replace characters that are problematic in filenames
    safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
    if len(safe_name) > 40:
        import hashlib
        h = hashlib.md5(str(cwd).encode()).hexdigest()[:8]
        safe_name = f"{safe_name[:30]}_{h}"
    return safe_name or "default"


# ── SessionStore ─────────────────────────────────────────


class SessionStore:
    """Persistent storage for conversation sessions."""

    def __init__(self, home_dir: str | Path = "~/.trove"):
        self.home_dir = Path(home_dir).expanduser().resolve()

    # ── Path utilities ───────────────────────────────────

    def _sessions_dir(self, project_name: str) -> Path:
        return self.home_dir / "sessions" / project_name

    def _session_db(self, project_name: str, session_id: str) -> Path:
        return self._sessions_dir(project_name) / f"{session_id}.db"

    def session_db_path(self, project_name: str, session_id: str) -> Path:
        """Public accessor for the per-session SQLite file (shared with TaskStore)."""
        return self._session_db(project_name, session_id)

    def _ensure_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    # ── Database initialization ──────────────────────────

    async def _init_db(self, db_path: Path) -> aiosqlite.Connection:
        """Open connection and create tables if needed."""
        self._ensure_dir(db_path.parent)
        conn = await aiosqlite.connect(str(db_path))
        await conn.execute(MESSAGES_TABLE_SQL)
        await conn.execute(META_TABLE_SQL)
        await conn.commit()
        return conn

    # ── CRUD: Create ─────────────────────────────────────

    async def create_session(
        self,
        project_cwd: str | Path = ".",
        user_id: str = "local",
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        """Create a new session and persist it.

        Args:
            project_cwd: Working directory used to derive the project name.
            user_id: Identifier for the user (default "local").
            metadata: Optional key-value metadata.

        Returns:
            A new Session object with the session persisted.
        """
        project_name = _normalize_project_name(project_cwd)
        session = Session(
            session_id=str(uuid.uuid4()),
            project_name=project_name,
            user_id=user_id,
            metadata=metadata or {},
        )

        db_path = self._session_db(project_name, session.session_id)
        conn = await self._init_db(db_path)

        # Store meta
        await conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("project_name", project_name),
        )
        await conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("user_id", user_id),
        )
        await conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("created_at", session.created_at.isoformat()),
        )
        await conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("summary", session.summary or ""),
        )
        await conn.commit()
        await conn.close()

        logger.debug("Created session %s in project %s", session.session_id, project_name)
        return session

    # ── CRUD: Read ───────────────────────────────────────

    async def load_session(
        self,
        session_id: str,
        project_cwd: str | Path = ".",
    ) -> Session:
        """Load an existing session from storage.

        Args:
            session_id: The session identifier.
            project_cwd: Working directory used to derive the project name.

        Returns:
            The loaded Session.

        Raises:
            SessionError: If the session does not exist.
        """
        project_name = _normalize_project_name(project_cwd)
        db_path = self._session_db(project_name, session_id)

        if not db_path.exists():
            raise SessionError(
                message=f"Session {session_id} not found",
                session_id=session_id,
                details={"project": project_name},
            )

        conn = await aiosqlite.connect(str(db_path))

        # Load meta
        meta = {}
        cursor = await conn.execute("SELECT key, value FROM meta")
        async for row in cursor:
            meta[row[0]] = row[1]

        # Load messages
        messages = []
        cursor = await conn.execute(
            "SELECT role, content, timestamp, metadata_json FROM messages ORDER BY id"
        )
        async for row in cursor:
            messages.append(Message(
                role=row[0],
                content=row[1],
                timestamp=datetime.fromisoformat(row[2]),
                metadata=json.loads(row[3]) if row[3] else {},
            ))

        await conn.close()

        return Session(
            session_id=session_id,
            project_name=meta.get("project_name", project_name),
            user_id=meta.get("user_id", "local"),
            messages=messages,
            summary=meta.get("summary") or None,
            branch_parent=meta.get("branch_parent") or None,
            created_at=datetime.fromisoformat(meta["created_at"])
                if "created_at" in meta
                else datetime.now(timezone.utc),
            metadata=json.loads(meta.get("metadata", "{}")),
        )

    # ── CRUD: Update ─────────────────────────────────────

    async def save_session(self, session: Session) -> None:
        """Persist a session's messages and metadata.

        This appends new messages (not yet in storage) to the database.
        Existing messages are not duplicated.

        Args:
            session: The session to persist.
        """
        db_path = self._session_db(session.project_name, session.session_id)
        if not db_path.exists():
            # Create if this is a new session
            conn = await self._init_db(db_path)
        else:
            conn = await aiosqlite.connect(str(db_path))

        # Count existing messages to know which ones are new
        cursor = await conn.execute("SELECT COUNT(*) FROM messages")
        row = await cursor.fetchone()
        existing_count = row[0] if row else 0

        # Insert only new messages
        new_messages = session.messages[existing_count:]
        for msg in new_messages:
            await conn.execute(
                "INSERT INTO messages (role, content, timestamp, metadata_json) VALUES (?, ?, ?, ?)",
                (
                    msg.role,
                    msg.content,
                    msg.timestamp.isoformat(),
                    json.dumps(msg.metadata, ensure_ascii=False),
                ),
            )

        # Update meta
        await conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("summary", session.summary or ""),
        )
        await conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("updated_at", datetime.now(timezone.utc).isoformat()),
        )

        session.updated_at = datetime.now(timezone.utc)
        await conn.commit()
        await conn.close()

    # ── CRUD: Delete ─────────────────────────────────────

    async def delete_session(
        self,
        session_id: str,
        project_cwd: str | Path = ".",
    ) -> bool:
        """Delete a session from storage.

        Returns:
            True if deleted, False if it didn't exist.
        """
        project_name = _normalize_project_name(project_cwd)
        db_path = self._session_db(project_name, session_id)

        if not db_path.exists():
            return False

        db_path.unlink()
        logger.debug("Deleted session %s", session_id)
        return True

    # ── CRUD: List ───────────────────────────────────────

    async def list_sessions(
        self,
        project_cwd: str | Path = ".",
    ) -> list[dict[str, Any]]:
        """List all sessions for a project.

        Returns:
            List of dicts with session_id, created_at, updated_at, message_count.
        """
        project_name = _normalize_project_name(project_cwd)
        sessions_dir = self._sessions_dir(project_name)

        if not sessions_dir.exists():
            return []

        results = []
        for db_file in sorted(sessions_dir.glob("*.db"), key=os.path.getmtime, reverse=True):
            sid = db_file.stem
            try:
                conn = await aiosqlite.connect(str(db_file))
                cursor = await conn.execute("SELECT COUNT(*) FROM messages")
                row = await cursor.fetchone()
                msg_count = row[0] if row else 0

                cursor = await conn.execute("SELECT value FROM meta WHERE key = 'created_at'")
                row = await cursor.fetchone()
                created_at = row[0] if row else ""

                cursor = await conn.execute("SELECT value FROM meta WHERE key = 'updated_at'")
                row = await cursor.fetchone()
                updated_at = row[0] if row else ""

                await conn.close()

                results.append({
                    "session_id": sid,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "message_count": msg_count,
                })
            except Exception as e:
                logger.warning("Skipping corrupt session db %s: %s", db_file, e)

        return results

    # ── Session operations ───────────────────────────────

    async def clear_session(self, session: Session) -> Session:
        """Remove all messages and the compaction summary, then persist.

        Keeps the session record itself (unlike delete_session).
        """
        db_path = self._session_db(session.project_name, session.session_id)
        conn = await self._init_db(db_path)
        await conn.execute("DELETE FROM messages")
        await conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("summary", ""),
        )
        await conn.commit()
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
        """Compact a session by replacing old messages with a summary.

        Keeps the most recent `keep_recent` message pairs (user + assistant)
        and replaces everything before them with a single summary message.

        Args:
            session: The session to compact.
            summary_text: LLM-generated conversation summary.
            keep_recent: Number of recent message pairs to keep.

        Returns:
            The compacted session (also persisted).
        """
        # Keep the last keep_recent * 2 messages (user+assistant pairs)
        keep_count = min(keep_recent * 2, len(session.messages))
        recent = session.messages[-keep_count:] if keep_count > 0 else []

        # Build new message list: summary + recent
        summary_msg = Message(
            role="system",
            content=f"[Conversation Summary]\n{summary_text}",
            metadata={"type": "compaction_summary"},
        )
        session.messages = [summary_msg] + recent
        session.summary = summary_text

        # Persist by rewriting messages in place (never unlink: the file
        # also carries the tasks table and meta keys like created_at/user_id)
        project_name = session.project_name
        db_path = self._session_db(project_name, session.session_id)
        conn = await self._init_db(db_path)
        await conn.execute("DELETE FROM messages")
        for msg in session.messages:
            await conn.execute(
                "INSERT INTO messages (role, content, timestamp, metadata_json) VALUES (?, ?, ?, ?)",
                (
                    msg.role,
                    msg.content,
                    msg.timestamp.isoformat(),
                    json.dumps(msg.metadata, ensure_ascii=False),
                ),
            )
        await conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("summary", summary_text),
        )
        await conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("project_name", project_name),
        )
        await conn.commit()
        await conn.close()

        logger.debug("Compacted session %s", session.session_id)
        return session
