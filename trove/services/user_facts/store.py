"""User facts database — ``~/.trove/user_facts.db``.

Independent per-user memory layer (Mem0-style) separate from the
datasource-level KB. A fact is a short user statement of a preference or
business caliber, scoped to ``(user_id, datasource)`` — e.g. "营收口径 = 净收入",
"看日均用 30 日均值". Facts are injected into SQL generation as a
personalization context block.

Follows the repo's aiosqlite conventions (open-per-operation, idempotent
``CREATE TABLE IF NOT EXISTS``, ISO-8601 text timestamps, additive-only
schema) — see ``trove/services/admin_settings/store.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from trove.core.logging import get_logger

logger = get_logger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


FACTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    datasource TEXT NOT NULL,
    fact TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "user_id": row[1],
        "datasource": row[2],
        "fact": row[3],
        "created_at": row[4],
        "updated_at": row[5],
    }


class UserFactsStore:
    """Raw CRUD over ``{home}/user_facts.db`` (StorageBackend-backed)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        from trove.storage.backends import resolve_backend

        self._backend = resolve_backend(str(db_path))
        self._schema_ready = False

    async def _conn(self):
        # 统一后端:生产 PG(TROVE_STORAGE_URL)/ 测试与本地 SQLite 内存库。
        # 连接由后端缓存管理(单连接复用),close() 为无操作避免每操作断连。
        await self._ensure_schema()
        return self._backend

    async def dispose(self) -> None:
        """Release the backend's connection (worker thread + file handle).

        aiosqlite 的 worker 线程是常驻非 daemon——不关闭,进程退出会
        挂住。测试 fixture teardown 与显式生命周期管理都应调用。
        """
        await self._backend.dispose()

    async def _ensure_schema(self) -> None:
        """幂等建表(首次连接时执行一次)。"""
        if self._schema_ready:
            return
        await self._backend.executescript(FACTS_TABLE_SQL)
        self._schema_ready = True

    async def add(self, user_id: str, datasource: str, fact: str) -> dict[str, Any]:
        """Insert a fact and return the stored row."""
        ts = now_iso()
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "INSERT INTO user_facts (user_id, datasource, fact, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, datasource, fact, ts, ts),
                need_lastrowid=True,
            )
            await conn.commit()
            fact_id = cursor.lastrowid
        finally:
            await conn.close()
        return await self.get(user_id, fact_id)

    async def get(self, user_id: str, fact_id: int) -> dict[str, Any] | None:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT id, user_id, datasource, fact, created_at, updated_at "
                "FROM user_facts WHERE id = ? AND user_id = ?",
                (fact_id, user_id),
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()
        return _row_to_dict(row) if row else None

    async def get_any(self, fact_id: int) -> dict[str, Any] | None:
        """Admin read: fetch a fact regardless of owner."""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT id, user_id, datasource, fact, created_at, updated_at "
                "FROM user_facts WHERE id = ?",
                (fact_id,),
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()
        return _row_to_dict(row) if row else None

    async def find_by_text(
        self, user_id: str, datasource: str, fact: str,
    ) -> dict[str, Any] | None:
        """已规范化等值事实查找(冲突消解):同 (user, datasource) 同文本。"""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT id, user_id, datasource, fact, created_at, updated_at "
                "FROM user_facts WHERE user_id = ? AND datasource = ? AND fact = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT 1",
                (user_id, datasource, fact),
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()
        return _row_to_dict(row) if row else None

    async def purge_expired(self, days: int) -> int:
        """物理删除超过 ``days`` 天未更新的事实(记忆压缩);返回删除条数。"""
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "DELETE FROM user_facts WHERE updated_at < ?",
                (cutoff.isoformat(),),
            )
            await conn.commit()
            return cursor.rowcount
        finally:
            await conn.close()

    async def list(
        self, user_id: str, datasource: str | None = None,
    ) -> list[dict[str, Any]]:
        """List a user's facts, optionally scoped to one datasource (mtime desc)."""
        conn = await self._conn()
        try:
            if datasource is None:
                cursor = await conn.execute(
                    "SELECT id, user_id, datasource, fact, created_at, updated_at "
                    "FROM user_facts WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
                    (user_id,),
                )
            else:
                cursor = await conn.execute(
                    "SELECT id, user_id, datasource, fact, created_at, updated_at "
                    "FROM user_facts WHERE user_id = ? AND datasource = ? "
                    "ORDER BY updated_at DESC, id DESC",
                    (user_id, datasource),
                )
            rows = await cursor.fetchall()
        finally:
            await conn.close()
        return [_row_to_dict(r) for r in rows]

    async def list_all(
        self, user_id: str | None = None, datasource: str | None = None,
    ) -> list[dict[str, Any]]:
        """Admin read: every fact (optionally filtered), mtime desc."""
        conn = await self._conn()
        try:
            where, params = [], []
            if user_id is not None:
                where.append("user_id = ?")
                params.append(user_id)
            if datasource is not None:
                where.append("datasource = ?")
                params.append(datasource)
            sql = (
                "SELECT id, user_id, datasource, fact, created_at, updated_at "
                "FROM user_facts"
                + (f" WHERE {' AND '.join(where)}" if where else "")
                + " ORDER BY updated_at DESC, id DESC"
            )
            cursor = await conn.execute(sql, tuple(params))
            rows = await cursor.fetchall()
        finally:
            await conn.close()
        return [_row_to_dict(r) for r in rows]

    async def update(
        self, user_id: str, fact_id: int, *, fact: str | None = None,
        datasource: str | None = None,
    ) -> dict[str, Any] | None:
        """Update a fact's text/datasource; returns None when not owned."""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT id FROM user_facts WHERE id = ? AND user_id = ?",
                (fact_id, user_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            fields, params = [], []
            if fact is not None:
                fields.append("fact = ?")
                params.append(fact)
            if datasource is not None:
                fields.append("datasource = ?")
                params.append(datasource)
            if fields:
                fields.append("updated_at = ?")
                params.append(now_iso())
                params.extend([fact_id, user_id])
                await conn.execute(
                    f"UPDATE user_facts SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
                    tuple(params),
                )
                await conn.commit()
        finally:
            await conn.close()
        return await self.get(user_id, fact_id)

    async def delete(self, user_id: str, fact_id: int) -> bool:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "DELETE FROM user_facts WHERE id = ? AND user_id = ?",
                (fact_id, user_id),
            )
            await conn.commit()
            return cursor.rowcount > 0
        finally:
            await conn.close()

    async def delete_any(self, fact_id: int) -> bool:
        """Admin delete: remove a fact regardless of owner."""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "DELETE FROM user_facts WHERE id = ?", (fact_id,)
            )
            await conn.commit()
            return cursor.rowcount > 0
        finally:
            await conn.close()
