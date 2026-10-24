"""MCP HTTP transport auth — the Bearer gate must be real, per-request, fail-closed.

The gate was a hollow shell from 2026-09-23 to 2026-10-24:
``_build_mcp_auth_middleware`` only handled the "no token" branch and implicitly
returned ``None`` otherwise, so a token-protected endpoint ran ungated. These
tests pin the behaviour that made it hollow, not the shape of the fix:

- every 401 also includes "wrong token", "revoked token" and "disabled user" —
  one body, so the response is not a token oracle;
- the static token is compared before the database is touched;
- the identity is resolved **per request**, so revoking a token takes effect on
  the next call instead of at process restart;
- a missing or broken auth service is a 503, never a pass-through.
"""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from trove.mcp.http_auth import (
    IDENTITY_STATE_KEY,
    MCPBearerAuthMiddleware,
    _parse_bearer,
)

TOKEN = "valid"


class _Downstream:
    """Inner ASGI app: records that it was reached, then answers 200."""

    def __init__(self) -> None:
        self.calls = 0
        self.scopes: list[dict] = []

    async def __call__(self, scope, receive, send):
        self.calls += 1
        self.scopes.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"downstream"})


class _ResolvingAuth:
    """``resolve_token`` stand-in. ``mapping`` is mutable so tests can revoke."""

    def __init__(self, mapping=None, *, error: Exception | None = None):
        self.mapping = dict(mapping or {})
        self.error = error
        self.calls: list[str] = []

    async def resolve_token(self, raw):
        self.calls.append(raw)
        if self.error is not None:
            raise self.error
        return self.mapping.get(raw)


async def _drive(token=TOKEN, auth=None, *, headers=(), scope_type="http"):
    downstream = _Downstream()
    middleware = MCPBearerAuthMiddleware(downstream, token=token, auth=auth)
    scope = {
        "type": scope_type,
        "method": "POST",
        "path": "/mcp",
        "headers": [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in headers
        ],
    }
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await middleware(scope, receive, send)
    status = next(
        (m["status"] for m in sent if m["type"] == "http.response.start"), None,
    )
    response_headers = next(
        (m.get("headers", []) for m in sent if m["type"] == "http.response.start"),
        [],
    )
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return {
        "scope": scope,
        "status": status,
        "headers": response_headers,
        "body": body,
        "downstream": downstream,
    }


def _header(sent: dict, name: bytes) -> bytes | None:
    for key, value in sent["headers"]:
        if key.lower() == name:
            return value
    return None


# ── credential parsing (pure) ─────────────────────────────────


@pytest.mark.parametrize("headers, expected", [
    ([], None),
    ([("Authorization", "Basic dXNlcjpwYXNz")], None),
    ([("Authorization", "Bearer")], None),
    ([("Authorization", "Bearer    ")], None),
    ([("Authorization", "Bearer abc")], "abc"),
    ([("Authorization", "bearer abc")], "abc"),
    ([("Authorization", "BEARER   abc  ")], "abc"),
    # 第一个 Authorization 头生效(与 Starlette Headers 同规则)
    ([("Authorization", "Bearer first"), ("Authorization", "Bearer second")], "first"),
])
def test_parse_bearer(headers, expected):
    raw = [(k.lower().encode(), v.encode()) for k, v in headers]
    assert _parse_bearer(raw) == expected


# ── the gate itself ───────────────────────────────────────────


@pytest.mark.parametrize("headers", [
    (),
    (("Authorization", "Basic dXNlcjpwYXNz"),),
    (("Authorization", "Bearer"),),
    (("Authorization", f"Bearer wrong-{TOKEN}"),),
    (("Authorization", f"bearer wrong-{TOKEN}"),),
    # 第一个头生效 → 即使第二个是对的也不放行
    (("Authorization", "Bearer wrong"), ("Authorization", f"Bearer {TOKEN}")),
])
async def test_rejected_credentials_never_reach_downstream(headers):
    auth = _ResolvingAuth({TOKEN: {"id": 1}})
    sent = await _drive(auth=auth, headers=headers)

    assert sent["status"] == 401
    assert sent["downstream"].calls == 0
    assert json.loads(sent["body"]) == {"detail": "unauthorized"}
    assert _header(sent, b"www-authenticate") == b"Bearer"
    # 垃圾 token 不打 DB:先 compare_digest,再 resolve
    assert auth.calls == []


async def test_valid_token_resolves_identity_into_scope_state():
    identity = {"id": 7, "role": "user", "username": "bob", "scopes": ["pii"]}
    auth = _ResolvingAuth({TOKEN: identity})
    sent = await _drive(auth=auth, headers=[("Authorization", f"Bearer {TOKEN}")])

    assert sent["status"] == 200
    assert sent["downstream"].calls == 1
    assert auth.calls == [TOKEN]
    assert sent["scope"]["state"][IDENTITY_STATE_KEY] == identity


async def test_token_is_resolved_on_every_request_so_revocation_is_immediate():
    identity = {"id": 7, "role": "admin"}
    auth = _ResolvingAuth({TOKEN: identity})
    headers = [("Authorization", f"Bearer {TOKEN}")]

    first = await _drive(auth=auth, headers=headers)
    assert first["status"] == 200

    # 撤销(同一个静态 token 仍匹配)—— 下一次请求必须 401
    auth.mapping.clear()
    second = await _drive(auth=auth, headers=headers)
    assert second["status"] == 401
    assert second["downstream"].calls == 0
    assert auth.calls == [TOKEN, TOKEN], "撤销没有在下一次请求生效"


async def test_resolution_error_is_503_not_fail_open():
    auth = _ResolvingAuth(error=RuntimeError("db down"))
    sent = await _drive(auth=auth, headers=[("Authorization", f"Bearer {TOKEN}")])

    assert sent["status"] == 503
    assert sent["downstream"].calls == 0
    assert IDENTITY_STATE_KEY not in (sent["scope"].get("state") or {})


async def test_missing_auth_service_is_503_not_fail_open():
    sent = await _drive(auth=None, headers=[("Authorization", f"Bearer {TOKEN}")])

    assert sent["status"] == 503
    assert sent["downstream"].calls == 0


async def test_non_http_scopes_pass_through_untouched():
    """lifespan/websocket 不是 MCP 端点:中间件不得拦启动。"""
    sent = await _drive(auth=None, headers=(), scope_type="lifespan")
    assert sent["status"] == 200
    assert sent["downstream"].calls == 1


# ── wiring: the CLI must build exactly this middleware ────────


def test_builder_returns_none_only_without_a_token():
    from trove.main import _build_mcp_auth_middleware

    assert _build_mcp_auth_middleware(None, object()) is None
    assert _build_mcp_auth_middleware("", object()) is None


def test_http_kwargs_wire_one_middleware_when_token_is_set():
    from trove.main import _mcp_http_kwargs

    auth = _ResolvingAuth()
    kwargs = _mcp_http_kwargs("http", "0.0.0.0", 8787, "/mcp", "abc", auth)

    assert kwargs["transport"] == "streamable-http"  # CLI alias normalized
    assert kwargs["host"] == "0.0.0.0" and kwargs["port"] == 8787
    assert kwargs["path"] == "/mcp"
    assert len(kwargs["middleware"]) == 1
    middleware = kwargs["middleware"][0]
    assert isinstance(middleware, Middleware)
    assert middleware.cls is MCPBearerAuthMiddleware
    assert middleware.kwargs == {"token": "abc", "auth": auth}


def test_http_kwargs_without_token_add_no_middleware():
    from trove.main import _mcp_http_kwargs

    kwargs = _mcp_http_kwargs("sse", "127.0.0.1", 8000, None, None, None)

    assert kwargs["transport"] == "sse"
    assert "path" not in kwargs
    assert "middleware" not in kwargs


async def test_starlette_instantiates_the_middleware_as_configured():
    """``starlette.middleware.Middleware`` 的实例化约定(类 + kwargs)。

    直接调中间件测不到这一层:``Middleware(cls, **kwargs)`` 的 kwargs 名错
    了要等到真正起服务才炸。这里用一个最小 Starlette 应用走真实栈。
    """
    from trove.main import _mcp_http_kwargs

    identity = {"id": 42, "role": "admin"}

    async def endpoint(request):
        current = getattr(request.state, "mcp_identity", None)
        return JSONResponse({"identity": current})

    kwargs = _mcp_http_kwargs("http", "127.0.0.1", 8000, "/mcp",
                              "abc", _ResolvingAuth({"abc": identity}))
    app = Starlette(
        routes=[Route("/mcp", endpoint, methods=["POST"])],
        middleware=kwargs["middleware"],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.post("/mcp", headers={"Authorization": "Bearer abc"})
        assert ok.status_code == 200
        assert ok.json()["identity"] == identity

        denied = await client.post("/mcp")
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == "Bearer"
        assert denied.json() == {"detail": "unauthorized"}
