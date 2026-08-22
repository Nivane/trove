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
