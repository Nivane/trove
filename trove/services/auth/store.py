"""Central application database — ``~/.trove/app.db``.

Owns all raw SQL for users, tokens, datasource grants and the audit log.
Policy lives in :class:`trove.services.auth.service.AuthService`; this store
is thin SQL only. Follows the repo's aiosqlite conventions (see
``trove/storage/task_store.py``): open-per-operation, idempotent
``CREATE TABLE IF NOT EXISTS``, ISO-8601 text timestamps, JSON in
``*_json`` TEXT columns. No migration framework — schema is additive-only.

``{home}/checkpoints.db`` (LangGraph checkpointer) is the precedent for a
single central DB under the Trove home directory.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from trove.core.logging import get_logger

logger = get_logger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Schema (idempotent) ───────────────────────────────────

USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    display_name TEXT NOT NULL DEFAULT '',
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

TOKENS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    expires_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_used_at TEXT
)
"""

USER_DATASOURCES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_datasources (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    datasource TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, datasource)
)
"""

AUDIT_LOG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id INTEGER,
    username TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    status INTEGER,
    details_json TEXT NOT NULL DEFAULT '{}'
)
"""

INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)",
]

# Column sets for row→dict mapping
USER_COLS = ("id", "username", "password_hash", "role", "display_name",
             "disabled", "created_at", "updated_at")
TOKEN_COLS = ("id", "token_hash", "user_id", "label", "expires_at",
              "revoked", "created_at", "last_used_at")
AUDIT_COLS = ("id", "ts", "user_id", "username", "action", "method",
              "path", "status", "details_json")


async def _fetch_all(cursor) -> list[tuple]:
    return [row async for row in cursor]


class AppDbStore:
    """Raw SQL access to the central ``app.db``."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    async def _conn(self) -> aiosqlite.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self.db_path))
        await conn.execute(USERS_TABLE_SQL)
        await conn.execute(TOKENS_TABLE_SQL)
        await conn.execute(USER_DATASOURCES_TABLE_SQL)
        await conn.execute(AUDIT_LOG_TABLE_SQL)
        for stmt in INDEX_SQL:
            await conn.execute(stmt)
        await conn.commit()
        return conn

    @staticmethod
    def _user_row(row: tuple) -> dict[str, Any]:
        return dict(zip(USER_COLS, row))

    @staticmethod
    def _token_row(row: tuple) -> dict[str, Any]:
        return dict(zip(TOKEN_COLS, row))

    @staticmethod
    def _audit_row(row: tuple) -> dict[str, Any]:
        d = dict(zip(AUDIT_COLS, row))
        try:
            d["details"] = json.loads(d["details_json"]) if d["details_json"] else {}
        except (TypeError, ValueError):
            d["details"] = {}
        del d["details_json"]
        return d

    # ── Users ─────────────────────────────────────────────

    async def create_user(
        self, username: str, password_hash: str, role: str = "user",
        display_name: str = "",
    ) -> dict[str, Any]:
        ts = now_iso()
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "INSERT INTO users (username, password_hash, role, display_name, "
                "disabled, created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (username, password_hash, role, display_name, ts, ts),
            )
            await conn.commit()
            return self._user_row((
                cursor.lastrowid, username, password_hash, role, display_name,
                0, ts, ts,
            ))
        finally:
            await conn.close()

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {', '.join(USER_COLS)} FROM users WHERE id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            return self._user_row(row) if row else None
        finally:
            await conn.close()

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {', '.join(USER_COLS)} FROM users WHERE username = ?", (username,)
            )
            row = await cursor.fetchone()
            return self._user_row(row) if row else None
        finally:
            await conn.close()

    async def list_users(self) -> list[dict[str, Any]]:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {', '.join(USER_COLS)} FROM users ORDER BY id"
            )
            return [self._user_row(row) async for row in cursor]
        finally:
            await conn.close()

    async def update_user(
        self, user_id: int, *, password_hash: str | None = None,
        role: str | None = None, display_name: str | None = None,
        disabled: int | None = None,
    ) -> dict[str, Any] | None:
        sets, values = ["updated_at = ?"], [now_iso()]
        if password_hash is not None:
            sets.append("password_hash = ?")
            values.append(password_hash)
        if role is not None:
            sets.append("role = ?")
            values.append(role)
        if display_name is not None:
            sets.append("display_name = ?")
            values.append(display_name)
        if disabled is not None:
            sets.append("disabled = ?")
            values.append(disabled)
        values.append(user_id)
        conn = await self._conn()
        try:
            await conn.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE id = ?", values
            )
            await conn.commit()
        finally:
            await conn.close()
        return await self.get_user_by_id(user_id)

    async def delete_user(self, user_id: int) -> bool:
        conn = await self._conn()
        try:
            cursor = await conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            await conn.commit()
            return cursor.rowcount > 0
        finally:
            await conn.close()

    async def count_users(self) -> int:
        conn = await self._conn()
        try:
            cursor = await conn.execute("SELECT COUNT(*) FROM users")
            row = await cursor.fetchone()
            return row[0] if row else 0
        finally:
            await conn.close()

    async def count_active_admins(self) -> int:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0"
            )
            row = await cursor.fetchone()
            return row[0] if row else 0
        finally:
            await conn.close()

    # ── Tokens ────────────────────────────────────────────

    async def insert_token(
        self, token_hash: str, user_id: int, label: str = "",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        ts = now_iso()
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "INSERT INTO tokens (token_hash, user_id, label, expires_at, "
                "revoked, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                (token_hash, user_id, label, expires_at, ts),
            )
            await conn.commit()
            return self._token_row((
                cursor.lastrowid, token_hash, user_id, label, expires_at, 0, ts, None,
            ))
        finally:
            await conn.close()

    async def get_token_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {', '.join(TOKEN_COLS)} FROM tokens WHERE token_hash = ?",
                (token_hash,),
            )
            row = await cursor.fetchone()
            return self._token_row(row) if row else None
        finally:
            await conn.close()

    async def list_tokens(self, user_id: int) -> list[dict[str, Any]]:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {', '.join(TOKEN_COLS)} FROM tokens WHERE user_id = ? "
                "ORDER BY created_at DESC",
                (user_id,),
            )
            return [self._token_row(row) async for row in cursor]
        finally:
            await conn.close()

    async def revoke_token(self, token_id: int) -> bool:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "UPDATE tokens SET revoked = 1 WHERE id = ?", (token_id,)
            )
            await conn.commit()
            return cursor.rowcount > 0
        finally:
            await conn.close()

    async def touch_token(self, token_id: int) -> None:
        conn = await self._conn()
        try:
            await conn.execute(
                "UPDATE tokens SET last_used_at = ? WHERE id = ?", (now_iso(), token_id)
            )
            await conn.commit()
        finally:
            await conn.close()

    # ── Datasource grants ─────────────────────────────────

    async def set_user_datasources(self, user_id: int, datasources: list[str]) -> None:
        conn = await self._conn()
        try:
            await conn.execute("DELETE FROM user_datasources WHERE user_id = ?", (user_id,))
            ts = now_iso()
            for ds in datasources:
                await conn.execute(
                    "INSERT INTO user_datasources (user_id, datasource, created_at) "
                    "VALUES (?, ?, ?)",
                    (user_id, ds, ts),
                )
            await conn.commit()
        finally:
            await conn.close()

    async def get_user_datasources(self, user_id: int) -> list[str]:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT datasource FROM user_datasources WHERE user_id = ? "
                "ORDER BY datasource",
                (user_id,),
            )
            return [row[0] async for row in cursor]
        finally:
            await conn.close()

    # ── Audit log ─────────────────────────────────────────

    async def append_audit(
        self, *, ts: str, user_id: int | None, username: str, action: str,
        method: str = "", path: str = "", status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        conn = await self._conn()
        try:
            await conn.execute(
                "INSERT INTO audit_log (ts, user_id, username, action, method, "
                "path, status, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, user_id, username, action, method, path, status,
                 json.dumps(details or {}, ensure_ascii=False)),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def list_audit(
        self, limit: int = 100, offset: int = 0,
        user_id: int | None = None, action: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses, values = [], []
        if user_id is not None:
            clauses.append("user_id = ?")
            values.append(user_id)
        if action:
            clauses.append("action = ?")
            values.append(action)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values += [limit, offset]
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {', '.join(AUDIT_COLS)} FROM audit_log {where} "
                "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                values,
            )
            return [self._audit_row(row) async for row in cursor]
        finally:
            await conn.close()

    async def count_audit(
        self, user_id: int | None = None, action: str | None = None,
    ) -> int:
        """Count audit rows matching the same filters as ``list_audit``.

        Kept as a separate COUNT query (same WHERE construction) so the
        list path keeps its LIMIT/OFFSET shape; total powers pagination UI.
        """
        clauses, values = [], []
        if user_id is not None:
            clauses.append("user_id = ?")
            values.append(user_id)
        if action:
            clauses.append("action = ?")
            values.append(action)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT COUNT(*) FROM audit_log {where}", values,
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
        finally:
            await conn.close()
