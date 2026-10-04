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
    updated_at TEXT NOT NULL,
    topic_grants_json TEXT
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
    last_used_at TEXT,
    scopes_json TEXT NOT NULL DEFAULT '[]'
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

LOGIN_ATTEMPTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL,
    ts TEXT NOT NULL
)
"""

INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_tokens_expires ON tokens(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_login_attempts_ts ON login_attempts(ts)",
]

# Column sets for row→dict mapping
USER_COLS = ("id", "username", "password_hash", "role", "display_name",
             "disabled", "created_at", "updated_at")

# 用户列表排序白名单:服务层的 sort 值 → 真实列名。绝不让请求值进 ORDER BY,
# 这里再做一层「列名只能是本字典里的常量」的防线。
USER_SORT_COLS = {
    "username": "username", "role": "role",
    "disabled": "disabled", "created_at": "created_at",
}
TOKEN_COLS = ("id", "token_hash", "user_id", "label", "expires_at",
              "revoked", "created_at", "last_used_at", "scopes_json")
AUDIT_COLS = ("id", "ts", "user_id", "username", "action", "method",
              "path", "status", "details_json")


async def _fetch_all(cursor) -> list[tuple]:
    return [row async for row in cursor]


def _like_pattern(text: str) -> str:
    """子串匹配的 LIKE 模式(大小写不敏感由 SQL 侧的 ``LOWER`` 完成)。

    ``%``/``_``/``\\`` 转义后 q 是**字面子串**而非通配模式 —— 搜
    ``user_1`` 不该把 ``userX1`` 也捞出来。SQL 里配套显式声明
    ``ESCAPE '\\'``:SQLite 默认没有转义符,显式声明后两个后端一致。
    """
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped.lower()}%"


def _parse_topic_grants(raw: Any) -> dict[str, list[str]] | None:
    """``topic_grants_json`` → 归一化字典;NULL = 未配置(``None``)。

    **读不懂的字节一律 → 空字典(该主体一个域都不见),不是 ``None``** ——
    空串也在其列:``None`` 在域层是「未收窄」,把一段读不出的内容翻译成放行
    是最坏的降级方向。写入侧只会写合法 JSON 或 SQL NULL(``json.dumps`` /
    列可空),``''`` 这类字节只可能来自人工改库 —— 那时严的方向才是对的。
    """
    if raw is None:
        return None
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    out: dict[str, list[str]] = {}
    for ds, topics in doc.items():
        if not isinstance(ds, str) or not ds:
            return {}
        if not isinstance(topics, list) or not all(
                isinstance(t, str) for t in topics):
            return {}
        out[ds] = list(topics)
    return out


def _users_filter(
    q: str | None = None, role: str | None = None, status: str | None = None,
) -> tuple[str, list[Any]]:
    """用户过滤的 ``WHERE`` 片段 + 参数(只用 SQLite/Postgres 通用语法)。

    status 语义:``active`` = 未禁用;``disabled`` = 已禁用;
    ``nogrant`` = 零数据源授权(相关子查询,两后端通用)。未知值抛
    ``ValueError`` —— 静默退化成「不过滤」会让调用方以为过滤器生效。
    """
    clauses: list[str] = []
    values: list[Any] = []
    if q:
        clauses.append(
            "(LOWER(username) LIKE ? ESCAPE '\\' "
            "OR LOWER(display_name) LIKE ? ESCAPE '\\')"
        )
        values += [_like_pattern(q), _like_pattern(q)]
    if role:
        clauses.append("role = ?")
        values.append(role)
    if status == "active":
        clauses.append("disabled = 0")
    elif status == "disabled":
        clauses.append("disabled = 1")
    elif status == "nogrant":
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM user_datasources ud "
            "WHERE ud.user_id = users.id)"
        )
    elif status:
        raise ValueError(f"invalid status: {status}")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, values


class AppDbStore:
    """Raw SQL access to the central ``app.db`` (StorageBackend-backed)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        from trove.storage.backends import resolve_backend

        self._backend = resolve_backend(str(db_path))
        self._schema_ready = False

    async def dispose(self) -> None:
        """Release the backend's connection (worker thread + file handle).

        aiosqlite 的 worker 线程是常驻非 daemon——不关闭,进程退出会
        挂住。测试 fixture teardown 与显式生命周期管理都应调用。
        """
        await self._backend.dispose()

    async def _conn(self):
        await self._ensure_schema()
        return self._backend

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        from trove.storage.backends.base import script_statements

        script = script_statements(
            [USERS_TABLE_SQL, TOKENS_TABLE_SQL, USER_DATASOURCES_TABLE_SQL,
             AUDIT_LOG_TABLE_SQL, LOGIN_ATTEMPTS_TABLE_SQL, *INDEX_SQL]
        )
        await self._backend.executescript(script)
        await self._ensure_token_scopes_column()
        await self._ensure_topic_grants_column()
        self._schema_ready = True

    async def _ensure_token_scopes_column(self) -> None:
        """存量库幂等补列 ``scopes_json``(schema additive-only,无迁移框架)。

        SQLite 不支持 ``ADD COLUMN IF NOT EXISTS``(仅 MySQL/Postgres),
        两条后端共用一条路径:直接 ALTER + 吞「列已存在」(SQLite 报
        duplicate column,Postgres 报 already exists)。其他错误照抛。
        """
        try:
            await self._backend.execute(
                "ALTER TABLE tokens ADD COLUMN scopes_json TEXT NOT NULL DEFAULT '[]'"
            )
            await self._backend.commit()
        except Exception as e:
            msg = str(e).lower()
            if "duplicate column" not in msg and "already exists" not in msg:
                raise

    async def _ensure_topic_grants_column(self) -> None:
        """存量库幂等补列 ``topic_grants_json``(同上手法)。

        注意与 ``scopes_json`` 的一处差异:**可空** —— ``NULL`` 在这里是有
        语义的取值(未配置域级收窄),不能给它 ``NOT NULL DEFAULT``。
        """
        try:
            await self._backend.execute(
                "ALTER TABLE users ADD COLUMN topic_grants_json TEXT"
            )
            await self._backend.commit()
        except Exception as e:
            msg = str(e).lower()
            if "duplicate column" not in msg and "already exists" not in msg:
                raise

    @staticmethod
    def _user_row(row: tuple) -> dict[str, Any]:
        return dict(zip(USER_COLS, row))

    @staticmethod
    def _token_row(row: tuple) -> dict[str, Any]:
        d = dict(zip(TOKEN_COLS, row))
        try:
            d["scopes"] = json.loads(d["scopes_json"]) if d["scopes_json"] else []
        except (TypeError, ValueError):
            d["scopes"] = []
        del d["scopes_json"]
        return d

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
                need_lastrowid=True,
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

    async def list_users_filtered(
        self, *, q: str | None = None, role: str | None = None,
        status: str | None = None, sort: str = "created_at",
        order: str = "desc", limit: int = 50, offset: int = 0,
    ) -> list[dict[str, Any]]:
        """过滤/排序/分页的用户行(参数由服务层校验,这里再白名单兜底)。

        ``id`` 始终作为末位排序键:role/disabled/created_at 都可能并列,
        没有确定性 tiebreaker 时翻页会漏行/重行。
        """
        where, values = _users_filter(q=q, role=role, status=status)
        if sort not in USER_SORT_COLS:
            raise ValueError(f"invalid sort: {sort}")
        if order not in ("asc", "desc"):
            raise ValueError(f"invalid order: {order}")
        direction = "ASC" if order == "asc" else "DESC"
        sql = (
            f"SELECT {', '.join(USER_COLS)} FROM users {where} "
            f"ORDER BY {USER_SORT_COLS[sort]} {direction}, id {direction} "
            "LIMIT ? OFFSET ?"
        )
        values += [limit, offset]
        conn = await self._conn()
        try:
            cursor = await conn.execute(sql, values)
            return [self._user_row(row) async for row in cursor]
        finally:
            await conn.close()

    async def count_users_filtered(
        self, *, q: str | None = None, role: str | None = None,
        status: str | None = None,
    ) -> int:
        """与 ``list_users_filtered`` 同一 WHERE 的计数(分页 total)。"""
        where, values = _users_filter(q=q, role=role, status=status)
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT COUNT(*) FROM users {where}", values,
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
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
        expires_at: str | None = None, scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        ts = now_iso()
        scopes_json = json.dumps(list(scopes or []), ensure_ascii=False)
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "INSERT INTO tokens (token_hash, user_id, label, expires_at, "
                "revoked, created_at, scopes_json) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (token_hash, user_id, label, expires_at, ts, scopes_json),
                need_lastrowid=True,
            )
            await conn.commit()
            return self._token_row((
                cursor.lastrowid, token_hash, user_id, label, expires_at, 0, ts,
                None, scopes_json,
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

    # ── Login attempts (brute-force lockout) ───────────────

    async def count_recent_failures(self, username: str, since_iso: str) -> int:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM login_attempts "
                "WHERE username = ? AND success = 0 AND ts >= ?",
                (username, since_iso),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
        finally:
            await conn.close()

    async def oldest_failure_ts(self, username: str, since_iso: str) -> str | None:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT MIN(ts) FROM login_attempts "
                "WHERE username = ? AND success = 0 AND ts >= ?",
                (username, since_iso),
            )
            row = await cursor.fetchone()
            return row[0] if row and row[0] else None
        finally:
            await conn.close()

    async def insert_login_attempt(self, username: str, ip: str, success: bool) -> None:
        conn = await self._conn()
        try:
            await conn.execute(
                "INSERT INTO login_attempts (username, ip, success, ts) "
                "VALUES (?, ?, ?, ?)",
                (username, ip, int(success), now_iso()),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def clear_login_failures(self, username: str) -> int:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "DELETE FROM login_attempts WHERE username = ? AND success = 0",
                (username,),
            )
            await conn.commit()
            return cursor.rowcount
        finally:
            await conn.close()

    # ── Hygiene (periodic purge) ───────────────────────────

    async def purge_expired_tokens(self) -> int:
        """Delete tokens whose ``expires_at`` has passed (ISO-8601 UTC
        strings compare lexicographically)."""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "DELETE FROM tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now_iso(),),
            )
            await conn.commit()
            return cursor.rowcount
        finally:
            await conn.close()

    async def purge_old_login_attempts(self, cutoff_iso: str) -> int:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "DELETE FROM login_attempts WHERE ts < ?", (cutoff_iso,)
            )
            await conn.commit()
            return cursor.rowcount
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

    # ── Topic grants(域级收窄;整表写入)─────────────────

    async def set_user_topic_grants(
        self, user_id: int, grants: dict[str, list[str]] | None,
    ) -> None:
        """整表写入;``None`` = 未配置收窄(列写 NULL,不是 ``'{}'``)。

        ``None`` 与 ``{}`` 是两种语义(不收窄 / 收窄到零),存储层必须分得
        开,所以列可空且空 dict 老老实实序列化成 ``'{}'``。
        """
        payload = None if grants is None else json.dumps(
            {str(ds): list(topics) for ds, topics in grants.items()},
            ensure_ascii=False)
        conn = await self._conn()
        try:
            await conn.execute(
                "UPDATE users SET topic_grants_json = ?, updated_at = ? "
                "WHERE id = ?",
                (payload, now_iso(), user_id),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def get_user_topic_grants(
        self, user_id: int,
    ) -> dict[str, list[str]] | None:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT topic_grants_json FROM users WHERE id = ?", (user_id,))
            row = await cursor.fetchone()
            if row is None:
                return None
            return _parse_topic_grants(row[0])
        finally:
            await conn.close()

    async def datasources_for_users(
        self, user_ids: list[int],
    ) -> dict[int, list[str]]:
        """整页用户的授权——**一次查询**取回(user_id → 数据源名,已排序)。

        管理台用户列表用它消灭按人循环的 N+1(逐人调
        ``get_user_datasources``)。空页直接返回,不发 ``IN ()``(两个
        后端的空 IN 都非法)。每个用户内部的顺序与逐人查询一致(按名排序)。
        """
        if not user_ids:
            return {}
        marks = ", ".join("?" * len(user_ids))
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT user_id, datasource FROM user_datasources "
                f"WHERE user_id IN ({marks}) ORDER BY user_id, datasource",
                list(user_ids),
            )
            out: dict[int, list[str]] = {}
            async for row in cursor:
                out.setdefault(int(row[0]), []).append(row[1])
            return out
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
