"""OIDC authorization-code + PKCE login.

The IdP is configured by an administrator (``agent.oidc`` / top-level
``oidc:``); nothing about it is taken from a request parameter, so there is
no SSRF surface. The only network calls are the issuer's discovery document,
its JWKS, and the token endpoint.

Design decisions worth keeping:

- **The existing opaque Bearer token is issued unchanged.** Downstream
  (middleware, MCP, sessions, jobs) never learns OIDC exists — the login
  flow's last step is exactly ``AuthService.create_token``.
- **Asymmetric signatures only.** ``none`` and ``HS*`` are rejected before
  any verification: a symmetric token would let anyone holding the public
  client id mint identities.
- **One-time state, server-side.** state / nonce / PKCE verifier live in
  ``app.db`` (``oidc_states``), consumed with a single ``DELETE … RETURNING``
  — process-local dicts break the moment a second instance answers the
  callback, and replay protection is the whole point of ``state``.
- **User-visible failures are a closed set of fragment codes.** The router
  redirects to ``#error=<code>``; details stay in the log/audit, never in a
  URL that gets bookmarked or shared.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from trove.core.config import OidcConfig
from trove.core.logging import get_logger

logger = get_logger(__name__)

#: 前端可展示的失败闭集(``#error=<code>``)。详情留在日志/审计里。
OIDC_ERROR_CODES = (
    "state_invalid", "exchange_failed", "conflict", "disabled",
    "not_provisioned",
)

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_DEFAULT_JWKS_TTL_S = 3600
_HTTP_TIMEOUT_S = 10.0


class OidcError(RuntimeError):
    """一次性 OIDC 失败。``code`` 属于 :data:`OIDC_ERROR_CODES`。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code if code in OIDC_ERROR_CODES else "exchange_failed"


def _is_https(url: str) -> bool:
    """https;localhost/127.0.0.1 的 http 例外(本机 dev IdP)。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme == "https":
        return bool(parts.netloc)
    return parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK_HOSTS


def validate_oidc_config(config: OidcConfig) -> list[str]:
    """配置问题清单(空 = 可用)。**启动时写日志、登录前拒绝**;不抛异常。"""
    problems: list[str] = []
    if not config.issuer:
        problems.append("issuer is required")
    elif not _is_https(config.issuer):
        problems.append(f"issuer must be https (or loopback http): {config.issuer}")
    if not config.client_id:
        problems.append("client_id is required")
    if config.redirect_uri and not _is_https(config.redirect_uri):
        problems.append(
            f"redirect_uri must be https (or loopback http): {config.redirect_uri}")
    if config.client_secret_ref and not config.client_secret_ref.startswith("env:"):
        problems.append("client_secret_ref must be env:NAME (never a literal secret)")
    if config.default_role not in ("admin", "analyst", "user"):
        problems.append(f"unknown default_role: {config.default_role}")
    for group, role in (config.role_map or {}).items():
        if role not in ("admin", "analyst", "user"):
            problems.append(f"role_map[{group!r}] has unknown role: {role}")
    return problems


def resolve_secret(ref: str) -> str:
    """``env:NAME`` → 环境变量值;空引用 = public client(无 secret)。

    其他写法返回空串 —— 校验侧已经把非 ``env:`` 引用报成配置问题,
    这里再把非引用当值使用就是「把密钥写进 YAML」的那条路。
    """
    if not ref:
        return ""
    if not ref.startswith("env:"):
        return ""
    return os.environ.get(ref[len("env:"):], "")


def redirect_uri_for(config: OidcConfig, base_url: str) -> str:
    """回调地址:显式配置优先;否则按请求 base URL 推导。"""
    if config.redirect_uri:
        return config.redirect_uri
    return (base_url or "").rstrip("/") + "/v1/auth/oidc/callback"


def sanitize_next(raw: str | None) -> str:
    """只允许站内相对路径。``//evil`` / 绝对 URL / 带 scheme 一律落回 ``/``。

    ``next`` 会进登录后的 fragment,开放重定向在这里关掉:重定向目标由
    同一个前端读取 token,把它指向外部站点就是把 token 交给外部站点。
    """
    path = (raw or "").strip()
    if not path:
        return "/"
    if not path.startswith("/") or path.startswith("//"):
        return "/"
    if "\\" in path or "\n" in path or "\r" in path:
        return "/"
    try:
        parts = urlsplit(path)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc:
        return "/"
    return path


def new_pkce() -> tuple[str, str]:
    """``(code_verifier, code_challenge)`` —— S256,无填充的 urlsafe base64。"""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@dataclass(frozen=True)
class OidcDiscovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


class OidcClient:
    """Discovery + JWKS + code exchange + ID-token verification.

    ``http`` / ``now`` are injectable so tests drive the whole flow with an
    ``httpx.MockTransport`` and a fake clock — zero network, deterministic
    expiry.
    """

    def __init__(
        self,
        config: OidcConfig,
        *,
        http: httpx.AsyncClient | None = None,
        now: Callable[[], float] = time.time,
        jwks_ttl_s: int = _DEFAULT_JWKS_TTL_S,
    ) -> None:
        self.config = config
        self._http = http
        self._owns_http = http is None
        self._now = now
        self._jwks_ttl_s = max(1, int(jwks_ttl_s))
        self._discovery: OidcDiscovery | None = None
        self._jwks: list[dict[str, Any]] | None = None
        self._jwks_at = 0.0

    # ── lifecycle ─────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
        return self._http

    async def dispose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # ── discovery / JWKS ───────────────────────────────────

    async def discovery(self) -> OidcDiscovery:
        if self._discovery is not None:
            return self._discovery
        url = self.config.issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            resp = await self._client().get(url)
            resp.raise_for_status()
            doc = resp.json()
        except Exception as e:  # noqa: BLE001 —— 统一翻译成 exchange_failed
            logger.warning("OIDC discovery failed for %s: %s", url, e)
            raise OidcError("exchange_failed", "discovery unavailable") from e
        declared = str(doc.get("issuer") or "").rstrip("/")
        if declared != self.config.issuer.rstrip("/"):
            # issuer 混用是典型配置错误(带/不带尾斜杠、tenant 路径写错);
            # 继续用它会让 iss 校验在另一个串上通过,等于没有校验。
            raise OidcError(
                "exchange_failed",
                f"discovery issuer mismatch: {declared!r} != {self.config.issuer!r}",
            )
        authz = str(doc.get("authorization_endpoint") or "")
        token = str(doc.get("token_endpoint") or "")
        jwks_uri = str(doc.get("jwks_uri") or "")
        if not (authz and token and jwks_uri):
            raise OidcError("exchange_failed", "discovery document incomplete")
        self._discovery = OidcDiscovery(
            issuer=declared, authorization_endpoint=authz,
            token_endpoint=token, jwks_uri=jwks_uri,
        )
        return self._discovery

    async def _fetch_jwks(self) -> list[dict[str, Any]]:
        disc = await self.discovery()
        try:
            resp = await self._client().get(disc.jwks_uri)
            resp.raise_for_status()
            doc = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("OIDC JWKS fetch failed for %s: %s", disc.jwks_uri, e)
            raise OidcError("exchange_failed", "JWKS unavailable") from e
        keys = doc.get("keys")
        if not isinstance(keys, list):
            raise OidcError("exchange_failed", "JWKS malformed")
        self._jwks = [k for k in keys if isinstance(k, dict)]
        self._jwks_at = self._now()
        return self._jwks

    async def _signing_jwks(self, *, force: bool) -> list[dict[str, Any]]:
        if force or self._jwks is None or (
                self._now() - self._jwks_at >= self._jwks_ttl_s):
            return await self._fetch_jwks()
        return self._jwks

    # ── flow ───────────────────────────────────────────────

    async def authorization_url(
        self, *, state: str, nonce: str, code_challenge: str,
        redirect_uri: str,
    ) -> str:
        disc = await self.discovery()
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": redirect_uri,
            "scope": self.config.scopes or "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        sep = "&" if "?" in disc.authorization_endpoint else "?"
        return disc.authorization_endpoint + sep + urlencode(params)

    async def exchange(self, *, code: str, code_verifier: str,
                       redirect_uri: str) -> dict[str, Any]:
        """授权码换 token。任何失败 → ``OidcError("exchange_failed")``。"""
        disc = await self.discovery()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.config.client_id,
            "code_verifier": code_verifier,
        }
        secret = resolve_secret(self.config.client_secret_ref)
        if secret:
            data["client_secret"] = secret
        try:
            resp = await self._client().post(
                disc.token_endpoint, data=data,
                headers={"Accept": "application/json"})
        except Exception as e:  # noqa: BLE001
            logger.warning("OIDC token exchange failed: %s", e)
            raise OidcError("exchange_failed", "token endpoint unreachable") from e
        if resp.status_code != 200:
            logger.warning(
                "OIDC token endpoint returned %s: %s",
                resp.status_code, resp.text[:200])
            raise OidcError("exchange_failed", "token exchange rejected")
        try:
            payload = resp.json()
        except ValueError as e:
            raise OidcError("exchange_failed", "token response is not JSON") from e
        if not isinstance(payload, dict) or not payload.get("id_token"):
            raise OidcError("exchange_failed", "token response has no id_token")
        return payload

    async def verify_id_token(
        self, raw: str, *, nonce: str,
    ) -> dict[str, Any]:
        """验证签名/iss/aud/exp/nonce → claims。**只接受非对称签名**。"""
        try:
            header = jwt.get_unverified_header(raw)
        except jwt.PyJWTError as e:
            raise OidcError("exchange_failed", "id_token header invalid") from e
        alg = str(header.get("alg") or "")
        if not alg or alg.lower() == "none" or alg.upper().startswith("HS"):
            raise OidcError(
                "exchange_failed", f"unsupported id_token alg: {alg!r}")
        kid = header.get("kid")

        key = None
        for force in (False, True):  # 未知 kid → 刷新一次 JWKS 再试
            jwks = await self._signing_jwks(force=force)
            key = _select_key(jwks, kid=kid, alg=alg)
            if key is not None:
                break
        if key is None:
            raise OidcError("exchange_failed", "no signing key matches id_token")

        try:
            claims = jwt.decode(
                raw, key=key, algorithms=[alg],
                audience=self.config.client_id,
                issuer=self.config.issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as e:
            raise OidcError("exchange_failed", f"id_token rejected: {e}") from e
        if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise OidcError("exchange_failed", "nonce mismatch")
        return claims


def _select_key(
    jwks: list[dict[str, Any]], *, kid: Any, alg: str,
) -> Any | None:
    """按 kid + alg 选一把公钥;kid 缺席时要求唯一匹配(歧义不猜)。"""
    candidates = []
    for jwk in jwks:
        if kid is not None and jwk.get("kid") != kid:
            continue
        key_alg = jwk.get("alg")
        if key_alg and str(key_alg) != alg:
            continue
        if str(jwk.get("kty") or "") not in ("RSA", "EC", "OKP"):
            continue
        candidates.append(jwk)
    if not candidates:
        return None
    try:
        if len(candidates) == 1:
            return jwt.PyJWK(candidates[0], algorithm=alg).key
        if kid is None:
            # 没有 kid 且不止一把可用键:不挑一把「看起来对」的 —— 拒绝。
            return None
        return jwt.PyJWK(candidates[0], algorithm=alg).key
    except jwt.PyJWTError:
        return None
