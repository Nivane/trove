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
    app = create_app(
        {"session_manager": object(), "connector_registry": _FakeRegistry()},
        allow_null_auth=True,
    )
    app.state.auth = auth
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
        _wait_until(lambda: auth.token_purges >= 1)
    assert auth.token_purges == 1  # one-shot at startup
    assert auth.attempt_purges == 1


# ── 行动提案过期腿(``trove.api.app._expire_actions``) ────
#
# 注意这条腿**不是**保留期清理:审批时限是行动层自己的钟,一条永远显示
# pending、实际没人能批的提案,正是这条腿存在的理由(审计谎言),所以它挂
# 在周期清扫上而不是"有 retention 才跑"。

class _FakeActions:
    def __init__(self) -> None:
        self.expires = 0

    async def expire_due(self) -> int:
        self.expires += 1
        return 1


def _actions_app(actions) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(actions=actions))


async def test_expire_actions_calls_expire_due():
    from trove.api.app import _expire_actions

    actions = _FakeActions()
    await _expire_actions(_actions_app(actions))
    assert actions.expires == 1


async def test_expire_actions_skips_missing_layer():
    from trove.api.app import _expire_actions

    await _expire_actions(_actions_app(None))  # no-op, no exception


async def test_expire_actions_swallows_failures():
    from trove.api.app import _expire_actions

    class _BrokenActions:
        async def expire_due(self) -> int:
            raise RuntimeError("boom")

    await _expire_actions(_actions_app(_BrokenActions()))  # never raises


def test_lifespan_periodic_sweep_expires_action_proposals(monkeypatch):
    """周期清扫真的走到行动过期腿。

    ``trove.api.app.asyncio`` IS the asyncio module —— 与 test_lifespan 的
    同一手法:临时把小时级 sleep 压短,非小时延迟回落到真实 sleep。
    """
    import asyncio

    from trove.core.config import RetentionConfig

    class _FakeMaintenance:
        async def run_all(self):
            return {}

    real_sleep = asyncio.sleep

    async def short_sleep(delay: float) -> None:
        await real_sleep(0.05 if delay >= 3600 else delay)

    monkeypatch.setattr("trove.api.app.asyncio.sleep", short_sleep)
    actions = _FakeActions()
    app = create_app({
        "session_manager": object(),
        "connector_registry": _FakeRegistry(),
        "config": SimpleNamespace(
            retention=RetentionConfig(sweep_interval_hours=1)),
        "maintenance": _FakeMaintenance(),
        "actions": actions,
    }, allow_null_auth=True)
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
        time.sleep(0.4)  # several periodic iterations at 0.05s/loop
    assert actions.expires >= 1
