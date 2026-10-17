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

#: OIDC 账号的密码占位符。**它不是合法 PBKDF2 串**(没有 ``$`` 分隔),
#: ``verify_password`` 解析失败即 False —— 本地密码登录对这些账号必定失败,
#: 且不需要给 users 表加「有没有密码」的新列。
OIDC_PASSWORD_SENTINEL = "!oidc"
#: 角色权限序(多个 IdP group 命中时取最高权限)。
OIDC_ROLE_RANK = {"user": 0, "analyst": 1, "admin": 2}


def map_oidc_role(claims: dict[str, Any], config: Any) -> str:
    """IdP group → 本地角色;多个命中取**最高权限**,无命中取默认值。

    映射是显式的:``role_map`` 里没有的 group 不参与判定。未知角色(配置
    写错)按 user 处理并已在 ``validate_oidc_config`` 报出 —— 这里不静默
    升级成更高权限。
    """
    groups = claims.get(getattr(config, "groups_claim", "groups"))
    if isinstance(groups, str):
        groups = [groups]
    if not isinstance(groups, (list, tuple, set)):
        groups = []
    role_map = getattr(config, "role_map", {}) or {}
    matched = [
        role_map[str(g)] for g in groups
        if str(g) in role_map and role_map[str(g)] in OIDC_ROLE_RANK
    ]
    if matched:
        return max(matched, key=lambda r: OIDC_ROLE_RANK[r])
    default = str(getattr(config, "default_role", "user") or "user")
    return default if default in OIDC_ROLE_RANK else "user"

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
        identities = await self.store.identities_for_users(
            [u["id"] for u in users])
        for user in users:
            user["datasources"] = grants.get(user["id"], [])
            bound = identities.get(user["id"], [])
            # auth_source 是**读出来的事实**(有没有绑定 identity),不是存储
            # 的字段:同一个账号可能既有密码又绑了 IdP,而密码哨兵本身不
            # 可反查。管理台只展示,不据此判定权限。
            user["identities"] = bound
            user["auth_source"] = "oidc" if bound else "local"
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

    # ── OIDC identities ────────────────────────────────────

    async def save_oidc_state(
        self, state: str, nonce: str, code_verifier: str,
        next_path: str, expires_at: str,
    ) -> None:
        await self.store.insert_oidc_state(
            state, nonce, code_verifier, next_path, expires_at)

    async def consume_oidc_state(self, state: str) -> dict[str, Any] | None:
        return await self.store.consume_oidc_state(state)

    async def purge_oidc_states(self, cutoff_iso: str) -> int:
        return await self.store.purge_oidc_states(cutoff_iso)

    async def identities_for_user(self, user_id: int) -> list[dict[str, Any]]:
        return await self.store.identities_for_user(user_id)

    async def resolve_oidc_user(
        self, *, issuer: str, subject: str, username: str,
        display_name: str, role: str, auto_provision: bool,
    ) -> dict[str, Any]:
        """身份 → 本地账号(必要时建号),并重算角色。

        - **已绑定 identity** → 直接复用该账号(哪怕用户名后来变了 ——
          稳定键是 ``(issuer, subject)``,不是用户名)。
        - **用户名已被本地账号占用且未绑定同一 identity** → 冲突。绝不
          静默合并:合并等于让 IdP 能接管一个已有本地账号。
        - 新号写哨兵密码、**零 grants**(数据源/主题域/行/列一律不给)。
        - 角色每次登录按 IdP group 重算;降级撞上「最后一个管理员」守卫时
          保留原角色并写审计(登录本身照常,守卫保护的是账号体系而不是
          这一次登录)。
        """
        from trove.services.auth.oidc import OidcError

        user_id = await self.store.get_identity(issuer, subject)
        if user_id is not None:
            row = await self.store.get_user_by_id(user_id)
            if row is None:
                # identity 还在、账号没了(人工改库 / 级联未生效):
                # 删掉这条孤儿绑定再走建号,绝不把身份指向一个不存在的 id。
                # (不删的话下面 insert_identity 会撞 UNIQUE(issuer, subject))
                await self.store.delete_identity(issuer, subject)
                user_id = None
            elif row["disabled"]:
                raise OidcError("disabled", "user is disabled")
            else:
                user = _public_user(row)
                return await self._sync_oidc_role(user, role)

        username = (username or "").strip()
        if not username:
            raise OidcError("not_provisioned", "identity has no username")
        existing = await self.store.get_user_by_username(username)
        if existing is not None:
            raise OidcError(
                "conflict", f"username already exists: {username}")
        if not auto_provision:
            raise OidcError(
                "not_provisioned", "auto-provision is disabled")

        try:
            row = await self.store.create_user(
                username, OIDC_PASSWORD_SENTINEL,
                role=role if role in OIDC_ROLE_RANK else "user",
                display_name=display_name or username,
            )
        except Exception:
            # 并发下用户名可能先被另一请求占用(唯一约束):报冲突,不 500。
            if await self.store.get_user_by_username(username) is not None:
                raise OidcError(
                    "conflict", f"username already exists: {username}") from None
            raise
        try:
            await self.store.insert_identity(issuer, subject, int(row["id"]))
        except Exception:
            # 同一身份的另一个并发回调先完成:删掉本次多建的号,复用已绑定
            # 的账号(稳定键是 (issuer, subject),不是用户名)。
            await self.store.delete_user(int(row["id"]))
            bound = await self.store.get_identity(issuer, subject)
            if bound is not None:
                bound_row = await self.store.get_user_by_id(bound)
                if bound_row is not None and not bound_row["disabled"]:
                    user = _public_user(bound_row)
                    return await self._sync_oidc_role(user, role)
            raise OidcError("conflict", "identity binding raced") from None
        return _public_user(row)

    async def _sync_oidc_role(
        self, user: dict[str, Any], role: str,
    ) -> dict[str, Any]:
        """登录时按 IdP 重算角色;守卫拒绝时保留原角色并写审计。

        返回**同步后**的用户 dict —— 调用方要的是这次登录的实际身份,
        不是更新前那一行。
        """
        if role not in OIDC_ROLE_RANK or role == user.get("role"):
            return user
        try:
            updated = await self.update_user(int(user["id"]), role=role)
        except AuthError as e:
            try:
                await self.record_audit(
                    "auth.oidc.role_rejected", user=user,
                    details={"wanted": role, "kept": user.get("role"),
                             "reason": str(e)[:200]},
                )
            except Exception:  # noqa: BLE001 —— 审计失败不改登录结果
                pass
            return user
        return updated or user

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

    # ── 行/列授权(收窄:未设置 = 不限制,设置后只会更窄)────────

    async def set_data_grants(
        self, user_id: int, datasource: str,
        row_filters: dict[str, str], blocked_columns: dict[str, list[str]],
    ) -> None:
        """按数据源整体替换;归一化(空白裁剪 / 去重 / 排序)只做一次。

        空表名/空谓词**抛错而不是跳过** —— 跳过等于把一条本想收窄的声明
        静默丢成「不过滤」,方向恰好是放宽。API 层已做解析校验,这里是
        直接调用方的兜底(同一 fail-closed 方向)。
        """
        rows: dict[str, str] = {}
        for table, predicate in (row_filters or {}).items():
            t, p = str(table).strip(), str(predicate).strip()
            if not t or not p:
                raise ValueError(f"行过滤的 {table!r} 缺表名或谓词")
            rows[t] = p
        cols = {
            str(t).strip(): sorted(
                {str(c).strip() for c in (columns or ()) if str(c).strip()})
            for t, columns in (blocked_columns or {}).items()
            if str(t).strip()
        }
        cols = {t: c for t, c in cols.items() if c}
        await self.store.set_data_grants(
            user_id, str(datasource).strip(), rows, cols)

    async def get_row_grants(
        self, user_id: int,
    ) -> dict[str, dict[str, str]] | None:
        return await self.store.get_row_grants(user_id)

    async def get_column_denials(
        self, user_id: int,
    ) -> dict[str, dict[str, list[str]]] | None:
        return await self.store.get_column_denials(user_id)

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
