"""Auth dependencies for API routes.

FastAPI dependency-based auth (not global middleware) keeps
Every protected endpoint adds ``user: dict = Depends(get_current_user)``
(or ``Depends(require_admin)``); the resolved user is also attached to
``request.state.user`` for audit.

``NullAuth`` is the fallback when a components dict lacks an ``auth``
service (embedded/stray ``create_app`` callers): every request runs as a
synthetic local admin and a loud warning is logged once. ``trove serve``
always injects a real AuthService, so this only exists to keep tests and
embedding code from hard-breaking.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, Header, HTTPException, Request

logger = logging.getLogger(__name__)

LOCAL_ADMIN = {
    "id": "local",
    "username": "local",
    "role": "admin",
    "display_name": "Local (auth disabled)",
    "disabled": False,
}


class NullAuth:
    """Stand-in auth service when no AuthService was injected."""

    async def resolve_token(self, raw_token: str) -> dict[str, Any] | None:
        return dict(LOCAL_ADMIN)

    async def get_datasources(self, user_id: int) -> list[str]:
        return []

    async def record_audit(self, *args, **kwargs) -> None:
        pass


def _get_auth(request: Request) -> Any:
    auth = getattr(request.app.state, "auth", None)
    if auth is None:
        auth = NullAuth()
        request.app.state.auth = auth
        logger.warning(
            "AUTH DISABLED — no auth service in components; "
            "requests run as synthetic local admin"
        )
    return auth


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Resolve the Bearer token to a user dict (401 on missing/invalid)."""
    auth = _get_auth(request)
    if isinstance(auth, NullAuth):
        request.state.user = dict(LOCAL_ADMIN)
        return request.state.user

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raw = authorization[len("Bearer "):].strip()
    user = await auth.resolve_token(raw)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="missing or invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.user = user
    return user


async def require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Admin-only guard (403 for non-admin roles)."""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="admin privileges required")
    return user


async def require_datasource(
    request: Request,
    datasource: str | None,
    user: dict[str, Any] = Depends(get_current_user),
) -> str:
    """Resolve and authorize a datasource.

    Admin: any datasource (explicit or the registry default).
    User: grants from the auth service — empty grants allow only the
    registry default (single-datasource deployments need no grant setup);
    non-empty grants are a strict allowlist.

    Returns the resolved datasource name (the router may pass the same
    value through its own resolution).
    """
    registry = getattr(request.app.state, "connector_registry", None)
    default_name = registry.default_name if registry is not None else None
    target = datasource or default_name
    if not target:
        raise HTTPException(status_code=400, detail="no active datasource")

    if user["role"] == "admin":
        return target

    auth = _get_auth(request)
    grants = await auth.get_datasources(user["id"])
    if grants:
        if target not in grants:
            raise HTTPException(status_code=403, detail=f"datasource not allowed: {target}")
    elif datasource and datasource != default_name:
        raise HTTPException(status_code=403, detail=f"datasource not allowed: {datasource}")
    return target


async def check_api_rate(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> None:
    """业务端点限流(按 user 的进程内令牌桶 + 日历日配额)。

    配置读 ``app.state.config``(admin 设置热更新即时生效);0 = 关闭。
    限流命中 → 429 + Retry-After。先过每分钟桶,再计日配额(被限流的
    请求不计入当日配额)。
    """
    limiter = getattr(request.app.state, "rate_limiter", None)
    config = getattr(request.app.state, "config", None)
    if limiter is None:
        return
    rpm = getattr(config, "api_rate_per_minute", 0) if config is not None else 0
    daily = getattr(config, "api_daily_quota", 0) if config is not None else 0
    if rpm <= 0 and daily <= 0:
        return
    key = f"user:{user['id']}"
    if rpm > 0:
        allowed, retry_after = limiter.allow(key, rpm)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="too many requests — slow down",
                headers={"Retry-After": str(retry_after)},
            )
    if daily > 0:
        allowed_daily, _reset = limiter.allow_daily(key, daily)
        if not allowed_daily:
            raise HTTPException(
                status_code=429,
                detail="daily request quota exceeded — try again tomorrow",
            )
