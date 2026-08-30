"""Runtime settings database — ``~/.trove/settings.db``.

Thin key/value store for admin-managed agent configuration (LLM model,
language, feature switches, retention). Values are JSON-encoded TEXT so one
table covers str / int / bool / list / dict. The DB overrides the agent.yml
baseline at boot and on every admin update — agent.yml is never written.

Follows the repo's aiosqlite conventions (open-per-operation, idempotent
``CREATE TABLE IF NOT EXISTS``, ISO-8601 text timestamps, additive-only
schema) — see ``trove/services/auth/store.py``.
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


SETTINGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class SettingsStore:
    """Raw key/value access to ``{home}/settings.db`` (StorageBackend-backed)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        from trove.storage.backends import resolve_backend

        self._backend = resolve_backend(str(db_path))
        self._schema_ready = False

    async def _conn(self):
        await self._ensure_schema()
        return self._backend

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        await self._backend.executescript(SETTINGS_TABLE_SQL)
        self._schema_ready = True

    async def get_all(self) -> dict[str, Any]:
        """Return every stored setting, JSON-decoded."""
        conn = await self._conn()
        try:
            cursor = await conn.execute("SELECT key, value FROM settings")
            rows = await cursor.fetchall()
        finally:
            await conn.close()
        out: dict[str, Any] = {}
        for key, raw in rows:
            try:
                out[key] = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("Ignoring malformed setting %s", key)
        return out

    async def get(self, key: str) -> Any | None:
        return (await self.get_all()).get(key)

    async def put_many(self, values: dict[str, Any]) -> None:
        """Upsert settings (only the provided keys are touched)."""
        if not values:
            return
        conn = await self._conn()
        try:
            for key, value in values.items():
                await conn.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), now_iso()),
                )
            await conn.commit()
        finally:
            await conn.close()

    async def delete(self, key: str) -> None:
        conn = await self._conn()
        try:
            await conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            await conn.commit()
        finally:
            await conn.close()

    async def delete_prefix(self, prefix: str) -> None:
        """Remove every key under a namespace (e.g. ``llm.`` to reset providers)."""
        conn = await self._conn()
        try:
            await conn.execute(
                "DELETE FROM settings WHERE key LIKE ?", (prefix + "%",)
            )
            await conn.commit()
        finally:
            await conn.close()