"""Authentication endpoints.

``POST /v1/auth/login`` is the single choke point where an external IdP
(SSO/OIDC) would later plug in: replace ``AuthService.authenticate`` (and
``resolve_token``) with IdP-backed validation behind the same Bearer
contract. Keep changes confined to those two seams.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import get_current_user
from trove.api.schemas import LoginRequest

router = APIRouter()


@router.post("/auth/login")
async def login(body: LoginRequest, request: Request) -> dict:
    auth = request.app.state.auth
    ip = request.client.host if request.client else ""
    allowed, retry_after = await auth.login_attempt_allowed(body.username)
    if not allowed:
        await auth.record_audit(
            "auth.login", method="POST", path="/v1/auth/login", status=429,
            details={"reason": "rate limited", "ip": ip},
        )
        raise HTTPException(
            status_code=429,
            detail=f"too many failed login attempts; try again in {retry_after}s",
            headers={"Retry-After": str(retry_after)},
        )
    user = await auth.authenticate(body.username, body.password)
    await auth.record_login_attempt(body.username, ip, success=user is not None)
    if user is None:
        await auth.record_audit(
            "auth.login", method="POST", path="/v1/auth/login", status=401,
            details={"reason": "invalid credentials"},
        )
        raise HTTPException(status_code=401, detail="invalid username or password")
    raw, record = await auth.create_token(
        user["id"], label="login", ttl_hours=auth.token_ttl_hours
    )
    await auth.record_audit(
        "auth.login", user=user, method="POST", path="/v1/auth/login", status=200
    )
    return {"token": raw, "expires_at": record["expires_at"], "user": user}


@router.get("/auth/me")
async def me(request: Request, user: dict = Depends(get_current_user)) -> dict:
    return {"user": user}


@router.post("/auth/logout")
async def logout(request: Request, user: dict = Depends(get_current_user)) -> dict:
    authorization = request.headers.get("authorization", "")
    raw = authorization[len("Bearer "):].strip() if authorization.lower().startswith("bearer ") else ""
    revoked = await request.app.state.auth.revoke_token_raw(raw)
    await request.app.state.auth.record_audit(
        "auth.logout", user=user, method="POST", path="/v1/auth/logout",
        status=200, details={"revoked": revoked},
    )
    return {"status": "ok"}
