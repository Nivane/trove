"""Bearer-token gate for the MCP HTTP transports (pure ASGI).

Why not Starlette's ``BaseHTTPMiddleware``: SSE responses are long-lived
streams. ``BaseHTTPMiddleware`` wraps the receive/send channels and buffers
response bodies, which can stall an event stream. A pure ASGI middleware
passes the channels through untouched.

The gate is deliberately fail-closed:

- a missing/malformed bearer token → 401;
- a token that no longer resolves (unknown / revoked / expired / disabled
  user) → 401, re-checked on **every request**, so revoking a token takes
  effect on the next call rather than at process restart;
- the auth service itself failing → 503 (never "let it through").

The constant-time comparison against the configured token happens first so
random credentials never reach the database. All 401s share one body so the
response cannot be used to tell "wrong token" from "revoked token".
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any, Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: Scope-state key read back by ``trove.mcp.server`` for per-request identity.
IDENTITY_STATE_KEY = "mcp_identity"


def _parse_bearer(headers: Iterable[tuple[bytes, bytes]]) -> str | None:
    """Return the bearer credential from an ASGI header list, or None.

    The first ``Authorization`` header wins (same as Starlette's ``Headers``);
    a non-Bearer scheme is rejected rather than tried.
    """
    for raw_name, raw_value in headers:
        if raw_name.lower() != b"authorization":
            continue
        try:
            value = raw_value.decode("latin-1").strip()
        except Exception:
            return None
        scheme, _, credential = value.partition(" ")
        if scheme.lower() != "bearer":
            return None
        credential = credential.strip()
        return credential or None
    return None


async def _deny(
    send: Send, status: int, detail: str, *, authenticate: bool = False,
) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if authenticate:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class MCPBearerAuthMiddleware:
    """ASGI middleware enforcing a Bearer token on every HTTP request.

    ``token`` is the configured ``--token`` (the static gate). ``auth`` is the
    process-wide ``AuthService`` used to resolve that token to a live user on
    each request; its absence is an error, not an open door.
    """

    def __init__(self, app: ASGIApp, *, token: str, auth: Any) -> None:
        self.app = app
        self.token = token
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # lifespan / websocket are not MCP endpoints; pass them through.
            await self.app(scope, receive, send)
            return

        raw = _parse_bearer(scope.get("headers") or ())
        if raw is None or not hmac.compare_digest(
            raw.encode("utf-8"), self.token.encode("utf-8"),
        ):
            await _deny(send, 401, "unauthorized", authenticate=True)
            return

        if self.auth is None:
            logger.error(
                "MCP bearer auth is configured but AuthService is unavailable — "
                "refusing every request",
            )
            await _deny(send, 503, "auth unavailable")
            return

        try:
            identity = await self.auth.resolve_token(raw)
        except Exception as e:
            # Do not log the credential or the exception message (it may echo
            # store internals); the type is enough to diagnose.
            logger.warning("MCP token resolution failed: %s", type(e).__name__)
            await _deny(send, 503, "auth unavailable")
            return

        if identity is None:
            await _deny(send, 401, "unauthorized", authenticate=True)
            return

        # Starlette's Request.state is backed by this same dict, so tools can
        # read request.state.mcp_identity via fastmcp's get_http_request().
        scope.setdefault("state", {})[IDENTITY_STATE_KEY] = identity
        await self.app(scope, receive, send)
