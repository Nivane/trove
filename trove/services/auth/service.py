"""Authentication & authorization service — policy over :class:`AppDbStore`.

Handles account CRUD (with admin guards), opaque Bearer token lifecycle,
datasource grants and the audit log. The raw-token→user mapping is the
single choke point where an external IdP (OIDC/SSO) could later plug in:
``resolve_token`` would then validate a JWT instead of looking up a stored
hash, behind the same Bearer contract. Keep that seam in mind.

A token is ``trove_<token_urlsafe(32)>``; only its sha256 hex is stored.
Password hashing is PBKDF2 (see :mod:`trove.services.auth.passwords`).
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trove.core.errors import AuthError, ErrorCode
from trove.services.auth.passwords import hash_password, verify_password
from trove.services.auth.store import AppDbStore, now_iso

DEFAULT_TOKEN_TTL_HOURS = 720  # 30 days
DEFAULT_LOGIN_MAX_ATTEMPTS = 5
DEFAULT_LOGIN_WINDOW_MINUTES = 15
GENERATED_PASSWORD_LEN = 20

# Keys safe to expose to clients (password_hash never leaves the service)
USER_PUBLIC_KEYS = ("id", "username", "role", "display_name", "disabled",
                    "created_at", "updated_at")

# ── Admin user-list query vocabulary ──────────────────────
# 用户枚举的单一事实来源:API 层的 400 文案、排序白名单都引用这里。
USER_ROLES = ("admin", "analyst", "user")
USER_STATUSES = ("active", "disabled", "nogrant")
USER_SORT_FIELDS = ("username", "role", "disabled", "created_at")
USER_ORDERS = ("asc", "desc")
USERS_DEFAULT_LIMIT = 50
USERS_MAX_LIMIT = 200


def _public_user(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row[k] for k in USER_PUBLIC_KEYS if k in row}
    if "disabled" in out:
        out["disabled"] = bool(out["disabled"])
    return out


def normalize_users_query(
    *, role: str | None = None, status: str | None = None,
    sort: str = "created_at", order: str = "desc",
    limit: int | None = None, offset: int = 0,
) -> dict[str, Any]:
    """校验并规范化管理台用户列表的查询参数。

    非法值抛 ``ValueError``(消息即原因,API 层映射为 400)。绝不静默降级:
    未知的 status/sort 一旦被当成「不过滤/默认排序」,调用方会以为过滤器
    生效,实际拿到全量——这正是项目哲学里要杜绝的静默失败。
    空字符串等价于未传(``?role=`` 与省略同义)。
    """
    role = role or None
    status = status or None
    sort = sort or "created_at"
    order = order or "desc"
    if role is not None and role not in USER_ROLES:
        raise ValueError(f"invalid role: {role} (allowed: {', '.join(USER_ROLES)})")
    if status is not None and status not in USER_STATUSES:
        raise ValueError(
            f"invalid status: {status} (allowed: {', '.join(USER_STATUSES)})"
        )
    if sort not in USER_SORT_FIELDS:
        raise ValueError(
            f"invalid sort: {sort} (allowed: {', '.join(USER_SORT_FIELDS)})"
        )
    if order not in USER_ORDERS:
        raise ValueError(f"invalid order: {order} (allowed: {', '.join(USER_ORDERS)})")
    size = USERS_DEFAULT_LIMIT if limit is None else limit
    if size < 1 or size > USERS_MAX_LIMIT:
        raise ValueError(f"invalid limit: {size} (allowed 1..{USERS_MAX_LIMIT})")
    if offset < 0:
        raise ValueError(f"invalid offset: {offset} (must be >= 0)")
    return {"role": role, "status": status, "sort": sort, "order": order,
            "limit": size, "offset": offset}


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _env_int(name: str, default: int) -> int:
    """Read a positive-int env knob (e.g. ``TROVE_TOKEN_TTL_HOURS``),
    falling back to ``default`` when unset or unparseable."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


class AuthService:
    """Account, token, grant and audit operations."""

    def __init__(self, db_path: str | Path):
        self.store = AppDbStore(db_path)
        self.token_ttl_hours = _env_int(
            "TROVE_TOKEN_TTL_HOURS", DEFAULT_TOKEN_TTL_HOURS
        )
        self.login_max_attempts = _env_int(
            "TROVE_LOGIN_MAX_ATTEMPTS", DEFAULT_LOGIN_MAX_ATTEMPTS
        )
        self.login_window_minutes = _env_int(
            "TROVE_LOGIN_WINDOW_MINUTES", DEFAULT_LOGIN_WINDOW_MINUTES
        )

    async def dispose(self) -> None:
        """Release the store's backend connection (see AppDbStore.dispose)."""
        await self.store.dispose()

    # ── Bootstrap ─────────────────────────────────────────

    async def ensure_bootstrap_admin(
        self, env_password: str | None = None,
    ) -> tuple[str, str | None]:
        """Create the initial admin when the users table is empty.

        Idempotent: with any user present this is a no-op that returns
        ``(username, None)`` and never overwrites an existing password.

        Returns:
            ``(username, generated_password_or_None)`` — the generated
            password is returned only on first creation (caller prints it
            once).
        """
        if await self.store.count_users() > 0:
            return ("admin", None)
        password = env_password or secrets.token_urlsafe(15)[:GENERATED_PASSWORD_LEN]
        await self.store.create_user(
            "admin", hash_password(password), role="admin", display_name="Administrator"
        )
        return ("admin", password)

    # ── Users ─────────────────────────────────────────────

    async def create_user(
        self, username: str, password: str, role: str = "user",
        display_name: str = "",
    ) -> dict[str, Any]:
        """Create a user. Raises AuthError(AUTH_USER_EXISTS) on duplicate."""
        username = username.strip()
        if not username:
            raise AuthError(
                ErrorCode.AUTH_INVALID_CREDENTIALS, "username must not be empty"
            )
        existing = await self.store.get_user_by_username(username)
        if existing:
            raise AuthError(ErrorCode.AUTH_USER_EXISTS, f"user already exists: {username}")
        row = await self.store.create_user(
            username, hash_password(password), role=role, display_name=display_name
        )
        return _public_user(row)

    async def list_users(self) -> list[dict[str, Any]]:
        return [_public_user(row) for row in await self.store.list_users()]

    async def list_users_page(
        self, *, q: str | None = None, role: str | None = None,
        status: str | None = None, sort: str = "created_at",
        order: str = "desc", limit: int | None = None, offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """管理台用户列表:过滤/排序/分页 + 内联数据源授权。

        返回 ``(users, total)``;``total`` 是**过滤后、分页前**的总数。
        每个用户条目 = 既有的公开字段 + ``datasources``(授权名,已排序),
        由整页用户**一次批量查询**取得——管理台不再按人 GET
        ``/users/{id}/datasources``(N+1)。

        非法查询参数抛 ``ValueError``(调用方映射为 400)。
        """
        spec = normalize_users_query(
            role=role, status=status, sort=sort, order=order,
            limit=limit, offset=offset,
        )
        q = (q or "").strip() or None
        rows = await self.store.list_users_filtered(q=q, **spec)
        total = await self.store.count_users_filtered(
            q=q, role=spec["role"], status=spec["status"],
        )
        users = [_public_user(row) for row in rows]
        grants = await self.store.datasources_for_users([u["id"] for u in users])
        for user in users:
            user["datasources"] = grants.get(user["id"], [])
        return users, total

    async def update_user(
        self, user_id: int, *, password: str | None = None,
        role: str | None = None, display_name: str | None = None,
        disabled: bool | None = None,
    ) -> dict[str, Any] | None:
        """Update a user. Returns None when the user doesn't exist.

        Guards: demoting/removing the last active admin is refused
        (``AuthError(AUTH_FORBIDDEN)``); a disabled admin still counts as
        active until disabled — the check runs before applying changes.
        """
        current = await self.store.get_user_by_id(user_id)
        if current is None:
            return None

        new_disabled = disabled if disabled is not None else bool(current["disabled"])
        new_role = role or current["role"]
        if (
            current["role"] == "admin" and bool(current["disabled"]) is False
            and (new_role != "admin" or new_disabled)
        ):
            admins = await self.store.count_active_admins()
            if admins <= 1:
                raise AuthError(
                    ErrorCode.AUTH_FORBIDDEN,
                    "cannot demote or disable the last active admin",
                )

        row = await self.store.update_user(
            user_id,
            password_hash=hash_password(password) if password is not None else None,
            role=role, display_name=display_name,
            disabled=int(disabled) if disabled is not None else None,
        )
        return _public_user(row) if row else None

    async def delete_user(self, user_id: int, *, actor_id: int) -> bool:
        """Delete a user. Guards: cannot delete yourself or the last admin."""
        current = await self.store.get_user_by_id(user_id)
        if current is None:
            return False
        if user_id == actor_id:
            raise AuthError(ErrorCode.AUTH_FORBIDDEN, "cannot delete your own account")
        if current["role"] == "admin" and not current["disabled"]:
            admins = await self.store.count_active_admins()
            if admins <= 1:
                raise AuthError(
                    ErrorCode.AUTH_FORBIDDEN, "cannot delete the last active admin"
                )
        return await self.store.delete_user(user_id)

    # ── Tokens ────────────────────────────────────────────

    async def create_token(
        self, user_id: int, label: str = "", ttl_hours: int | None = None,
        scopes: list[str] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Issue an opaque Bearer token. Returns ``(raw_token, record)`` —
        the raw token is shown exactly once.

        Args:
            scopes: Token 作用域 allowlist(如 ``["query"]``)。None/空列表 =
                不限(等价用户全部权限,存量 token 行为);非空 = 受限,
                只放行声明了所需 scope 的路由(路由级依赖,见
                ``api.deps.require_scope``)。
        """
        expires_at = None
        if ttl_hours is not None:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
            ).isoformat()
        raw = "trove_" + secrets.token_urlsafe(32)
        record = await self.store.insert_token(
            _hash_token(raw), user_id, label=label, expires_at=expires_at,
            scopes=scopes,
        )
        return raw, record

    async def revoke_token(self, token_id: int) -> bool:
        return await self.store.revoke_token(token_id)

    async def revoke_token_raw(self, raw_token: str) -> bool:
        """Revoke a token by its raw string (used by POST /v1/auth/logout)."""
        if not raw_token:
            return False
        record = await self.store.get_token_by_hash(_hash_token(raw_token))
        if record is None:
            return False
        return await self.store.revoke_token(record["id"])

    async def list_tokens(self, user_id: int) -> list[dict[str, Any]]:
        return await self.store.list_tokens(user_id)

    # ── Auth flows ────────────────────────────────────────

    async def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        """Password login. Returns the public user dict or None."""
        row = await self.store.get_user_by_username(username.strip())
        if row is None or row["disabled"]:
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return _public_user(row)

    # ── Login rate limiting ────────────────────────────────

    async def login_attempt_allowed(self, username: str) -> tuple[bool, int]:
        """True when a login attempt may proceed; when False, the second
        element is the Retry-After seconds (computed from the oldest
        in-window failure, so the lock expires when that failure ages out
        rather than after a full fresh window)."""
        name = username.strip()
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(minutes=self.login_window_minutes)
        ).isoformat()
        count = await self.store.count_recent_failures(name, cutoff)
        if count < self.login_max_attempts:
            return True, 0
        retry_after = self.login_window_minutes * 60
        oldest = await self.store.oldest_failure_ts(name, cutoff)
        if oldest:
            try:
                remaining = (
                    datetime.fromisoformat(oldest)
                    + timedelta(minutes=self.login_window_minutes)
                    - datetime.now(timezone.utc)
                ).total_seconds()
                retry_after = max(1, min(retry_after, int(remaining)))
            except ValueError:
                pass
        return False, retry_after

    async def record_login_attempt(
        self, username: str, ip: str = "", success: bool = False,
    ) -> None:
        """Persist a login attempt; a successful login clears prior
        failures so a user who recovers the password is never stuck
        locked until the window ages out."""
        name = username.strip()
        await self.store.insert_login_attempt(name, ip, success)
        if success:
            await self.store.clear_login_failures(name)

    async def purge_expired_tokens(self) -> int:
        """Delete tokens past their ``expires_at`` (housekeeping; the
        hard enforcement is ``resolve_token`` on every request)."""
        return await self.store.purge_expired_tokens()

    async def purge_old_login_attempts(self) -> int:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(minutes=self.login_window_minutes)
        ).isoformat()
        return await self.store.purge_old_login_attempts(cutoff)

    async def resolve_token(self, raw_token: str) -> dict[str, Any] | None:
        """Resolve a Bearer token to a user, or None (unknown/revoked/
        expired/disabled user). Touches last_used_at on success.

        Returns the public user dict plus ``scopes`` (token 作用域;空列表 =
        不限)——路由级 scope 依赖据此做最小权限裁决。
        """
        if not raw_token:
            return None
        record = await self.store.get_token_by_hash(_hash_token(raw_token))
        if record is None or record["revoked"]:
            return None
        if record["expires_at"]:
            try:
                expires = datetime.fromisoformat(record["expires_at"])
            except ValueError:
                expires = None
            if expires is not None and expires <= datetime.now(timezone.utc):
                return None
        user = await self.store.get_user_by_id(record["user_id"])
        if user is None or user["disabled"]:
            return None
        await self.store.touch_token(record["id"])
        public = _public_user(user)
        public["scopes"] = list(record.get("scopes") or [])
        return public

    # ── Datasource grants ─────────────────────────────────

    async def set_datasources(self, user_id: int, datasources: list[str]) -> None:
        await self.store.set_user_datasources(user_id, datasources)

    async def get_datasources(self, user_id: int) -> list[str]:
        return await self.store.get_user_datasources(user_id)

    # ── Topic grants(数据源之下的第二层:主题域可见性)──────

    async def set_topic_grants(
        self, user_id: int, grants: dict[str, list[str]] | None,
    ) -> None:
        """整表写入域级授权;``None`` = 取消收窄,``{}`` = 显式收窄到零。

        归一化(空白裁剪 / 去重 / 排序)只在这里做一次:写入即唯一形态 ——
        读回、审计与判定看到的都是同一份,不靠调用方自律。
        """
        if grants is None:
            await self.store.set_user_topic_grants(user_id, None)
            return
        normalized = {
            str(ds).strip(): sorted(
                {str(t).strip() for t in (topics or ()) if str(t).strip()})
            for ds, topics in grants.items()
            if str(ds).strip()
        }
        await self.store.set_user_topic_grants(user_id, normalized)

    async def get_topic_grants(self, user_id: int) -> dict[str, list[str]] | None:
        return await self.store.get_user_topic_grants(user_id)

    # ── Audit ─────────────────────────────────────────────

    async def record_audit(
        self, action: str, user: dict[str, Any] | None = None,
        method: str = "", path: str = "", status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append an audit entry. ``user`` may be None (e.g. failed login)."""
        await self.store.append_audit(
            ts=now_iso(),
            user_id=user.get("id") if user else None,
            username=(user or {}).get("username", ""),
            action=action, method=method, path=path, status=status,
            details=details,
        )

    async def list_audit(
        self, limit: int = 100, offset: int = 0,
        user_id: int | None = None, action: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.store.list_audit(
            limit=min(max(limit, 1), 500), offset=max(offset, 0),
            user_id=user_id, action=action,
        )

    async def count_audit(
        self, user_id: int | None = None, action: str | None = None,
    ) -> int:
        """Total audit rows for the given filters (pagination support)."""
        return await self.store.count_audit(user_id=user_id, action=action)
