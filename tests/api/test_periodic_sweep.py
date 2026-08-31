"""Auth-hygiene purge wiring (``trove.api.app._purge_auth``).

The api suite's ASGITransport does not trigger lifespan events; the
lifespan-tied startup purge is exercised with starlette's TestClient
(sync, portal thread — do not mark pytest.mark.asyncio), while the
purge helper itself is covered with async tests on a SimpleNamespace app.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from trove.api.app import _purge_auth, create_app


class _FakeAuth:
    def __init__(self) -> None:
        self.token_purges = 0
        self.attempt_purges = 0

    async def purge_expired_tokens(self) -> int:
        self.token_purges += 1
        return 3

    async def purge_old_login_attempts(self) -> int:
        self.attempt_purges += 1
        return 2


class _FakeRegistry:
    """Satisfies the /v1/health datasource probe (list_names → no pings)."""

    def list_names(self) -> list[str]:
        return []


def _app(auth) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(auth=auth))


async def test_purge_auth_calls_both_purges():
    auth = _FakeAuth()
    await _purge_auth(_app(auth))
    assert auth.token_purges == 1
    assert auth.attempt_purges == 1


async def test_purge_auth_skips_missing_auth():
    await _purge_auth(_app(None))  # no exception, no-op


async def test_purge_auth_skips_nullauth_like_object():
    await _purge_auth(_app(object()))  # no purge methods → no-op


async def test_purge_auth_swallows_failures():
    class _BrokenAuth:
        async def purge_expired_tokens(self) -> int:
            raise RuntimeError("boom")

    await _purge_auth(_app(_BrokenAuth()))  # never raises


def _wait_until(predicate, timeout_s: float = 0.5) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not reached within {timeout_s:.1f}s")
        time.sleep(0.01)


def test_lifespan_startup_runs_auth_purge():
    """Startup spawns a best-effort auth purge task even without a
    maintenance component (auth hygiene must not depend on retention)."""
    auth = _FakeAuth()
    app = create_app({"session_manager": object(), "connector_registry": _FakeRegistry()})
    app.state.auth = auth
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
        _wait_until(lambda: auth.token_purges >= 1)
    assert auth.token_purges == 1  # one-shot at startup
    assert auth.attempt_purges == 1
