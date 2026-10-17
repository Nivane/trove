"""Authentication endpoints.

``POST /v1/auth/login`` is the password choke point; OIDC (SSO) plugs in
beside it under ``/v1/auth/oidc/*`` and issues the **same opaque Bearer
token** — downstream code never learns which door the user came through.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from trove.api.deps import get_current_user
from trove.api.schemas import LoginRequest
from trove.services.auth.oidc import (
    OidcClient,
    OidcError,
    new_pkce,
    redirect_uri_for,
    sanitize_next,
)
from trove.services.auth.service import map_oidc_role

router = APIRouter()

#: 登录 state 的有效期(一次性行,过期即在回调时判 state_invalid)。
_OIDC_STATE_TTL = timedelta(minutes=10)


def _oidc(request: Request) -> OidcClient | None:
    return getattr(request.app.state, "oidc", None)


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


# ── OIDC authorization-code login ─────────────────────────
#
# 未装配 OidcClient(配置关闭 / 配置不完整)→ 404:没有按钮、没有路由,
# 与「从未支持 SSO」逐字节相同。用户可见的失败一律 303 到
# ``post_login_redirect#error=<code>``;细节只进日志与审计。


def _error_redirect(request: Request, code: str) -> RedirectResponse:
    config = _oidc(request).config
    target = (config.post_login_redirect or "/").strip() or "/"
    return RedirectResponse(f"{target}#{urlencode({'error': code})}", status_code=303)


async def _audit(request: Request, action: str, *, status: int,
                 details: dict | None = None) -> None:
    """best-effort 审计:细节**不含** token / code / ID token。"""
    auth = getattr(request.app.state, "auth", None)
    if auth is None or not hasattr(auth, "record_audit"):
        return
    try:
        await auth.record_audit(
            action, method=request.method, path=request.url.path,
            status=status, details=details or {},
        )
    except Exception:  # noqa: BLE001 —— 审计不能拖垮登录
        pass


@router.get("/auth/oidc/status")
async def oidc_status(request: Request) -> dict:
    if _oidc(request) is None:
        raise HTTPException(status_code=404, detail="OIDC is not enabled")
    return {
        "enabled": True,
        "button_label": "SSO",
        "login_url": "/v1/auth/oidc/login",
    }


@router.get("/auth/oidc/login")
async def oidc_login(request: Request, next: str = "/") -> RedirectResponse:
    oidc = _oidc(request)
    auth = getattr(request.app.state, "auth", None)
    if oidc is None or auth is None or not hasattr(auth, "save_oidc_state"):
        raise HTTPException(status_code=404, detail="OIDC is not enabled")

    now = datetime.now(timezone.utc)
    # 顺手清理过期 state(无独立定时器也能收敛;多实例下每个实例都会做)。
    try:
        await auth.purge_oidc_states(now.isoformat())
    except Exception:  # noqa: BLE001 —— 清理失败不影响本次登录
        pass

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = new_pkce()
    await auth.save_oidc_state(
        state, nonce, verifier, sanitize_next(next),
        (now + _OIDC_STATE_TTL).isoformat(),
    )
    try:
        url = await oidc.authorization_url(
            state=state, nonce=nonce, code_challenge=challenge,
            redirect_uri=redirect_uri_for(
                oidc.config, str(request.base_url)),
        )
    except OidcError as e:
        await _audit(request, "auth.oidc.login", status=502,
                     details={"error": e.code, "stage": "discovery"})
        return _error_redirect(request, e.code)
    return RedirectResponse(url, status_code=302)


@router.get("/auth/oidc/callback")
async def oidc_callback(
    request: Request, code: str = "", state: str = "", error: str = "",
) -> RedirectResponse:
    oidc = _oidc(request)
    auth = getattr(request.app.state, "auth", None)
    if oidc is None or auth is None or not hasattr(auth, "consume_oidc_state"):
        raise HTTPException(status_code=404, detail="OIDC is not enabled")

    state_row = await auth.consume_oidc_state(state) if state else None
    if state_row is None:
        await _audit(request, "auth.oidc.login", status=400,
                     details={"error": "state_invalid"})
        return _error_redirect(request, "state_invalid")
    try:
        expires = datetime.fromisoformat(str(state_row.get("expires_at")))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        expired = expires < datetime.now(timezone.utc)
    except ValueError:
        expired = True
    if expired:
        await _audit(request, "auth.oidc.login", status=400,
                     details={"error": "state_invalid", "reason": "expired"})
        return _error_redirect(request, "state_invalid")
    if error or not code:
        await _audit(request, "auth.oidc.login", status=400,
                     details={"error": "exchange_failed",
                              "reason": (error or "no code")[:100]})
        return _error_redirect(request, "exchange_failed")

    try:
        tokens = await oidc.exchange(
            code=code, code_verifier=state_row["code_verifier"],
            redirect_uri=redirect_uri_for(oidc.config, str(request.base_url)),
        )
        claims = await oidc.verify_id_token(
            str(tokens["id_token"]), nonce=state_row["nonce"])
        subject = str(claims.get("sub") or "")
        username = str(
            claims.get("preferred_username")
            or claims.get("email") or subject)
        display_name = str(claims.get("name") or username)
        user = await auth.resolve_oidc_user(
            issuer=oidc.config.issuer, subject=subject,
            username=username, display_name=display_name,
            role=map_oidc_role(claims, oidc.config),
            auto_provision=bool(oidc.config.auto_provision),
        )
    except OidcError as e:
        await _audit(request, "auth.oidc.login", status=403,
                     details={"error": e.code})
        return _error_redirect(request, e.code)

    raw, _record = await auth.create_token(
        user["id"], label="oidc", ttl_hours=auth.token_ttl_hours)
    await _audit(request, "auth.oidc.login", status=200,
                 details={"user_id": user["id"]})
    frag = urlencode({"token": raw, "next": state_row.get("next_path") or "/"})
    target = (oidc.config.post_login_redirect or "/").strip() or "/"
    return RedirectResponse(f"{target}#{frag}", status_code=303)


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
