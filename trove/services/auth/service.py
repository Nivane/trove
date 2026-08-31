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


def _public_user(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row[k] for k in USER_PUBLIC_KEYS if k in row}
    if "disabled" in out:
        out["disabled"] = bool(out["disabled"])
    return out


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
    ) -> tuple[str, dict[str, Any]]:
        """Issue an opaque Bearer token. Returns ``(raw_token, record)`` —
        the raw token is shown exactly once."""
        expires_at = None
        if ttl_hours is not None:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
            ).isoformat()
        raw = "trove_" + secrets.token_urlsafe(32)
        record = await self.store.insert_token(
            _hash_token(raw), user_id, label=label, expires_at=expires_at
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
        expired/disabled user). Touches last_used_at on success."""
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
        return _public_user(user)

    # ── Datasource grants ─────────────────────────────────

    async def set_datasources(self, user_id: int, datasources: list[str]) -> None:
        await self.store.set_user_datasources(user_id, datasources)

    async def get_datasources(self, user_id: int) -> list[str]:
        return await self.store.get_user_datasources(user_id)

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
