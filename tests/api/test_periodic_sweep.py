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


# ── 行动自动重试腿(``trove.api.app._retry_actions``)────────
#
# 这条腿挂在 30s 的 tick 上(不是小时级的周期清扫):失败重试的退避以秒计,
# 挂在小时级清扫上等于把"自动重试"变成"自动明天再试"。默认档关 —— 没配
# retry_backoff_base_s 时服务侧立即返回 0,连库都不查。

class _FakeRetryActions:
    def __init__(self, *, armed: bool = True) -> None:
        self.calls = 0
        self.guards = SimpleNamespace(
            retry_backoff_base_s=60 if armed else 0)

    async def retry_due_proposals(self) -> int:
        self.calls += 1
        return 1


async def test_retry_actions_calls_the_service():
    from trove.api.app import _retry_actions

    actions = _FakeRetryActions()
    await _retry_actions(_actions_app(actions))
    assert actions.calls == 1


async def test_retry_actions_skips_missing_layer():
    from trove.api.app import _retry_actions

    await _retry_actions(_actions_app(None))  # no-op, no exception


async def test_retry_actions_swallows_failures():
    from trove.api.app import _retry_actions

    class _BrokenActions:
        async def retry_due_proposals(self) -> int:
            raise RuntimeError("boom")

    await _retry_actions(_actions_app(_BrokenActions()))  # never raises


def test_retry_armed_follows_the_guard_config():
    from trove.api.app import _retry_armed

    assert _retry_armed(_FakeRetryActions(armed=True)) is True
    assert _retry_armed(_FakeRetryActions(armed=False)) is False
    assert _retry_armed(None) is False
    assert _retry_armed(object()) is False  # 没有 guards 的对象不炸


def test_lifespan_job_tick_runs_the_retry_leg(monkeypatch):
    """tick 的两条腿共享同一个循环:没有 scheduler 时,配了退避也要跑重试。"""
    import asyncio

    from trove.core.config import RetentionConfig

    real_sleep = asyncio.sleep

    async def short_sleep(delay: float) -> None:
        await real_sleep(0.05 if delay >= 1 else delay)

    monkeypatch.setattr("trove.api.app.asyncio.sleep", short_sleep)
    actions = _FakeRetryActions(armed=True)
    app = create_app({
        "session_manager": object(),
        "connector_registry": _FakeRegistry(),
        "config": SimpleNamespace(
            scheduler_poll_seconds=1,
            retention=RetentionConfig(sweep_interval_hours=0)),
        "actions": actions,
    }, allow_null_auth=True)
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
        time.sleep(0.4)
    assert actions.calls >= 1


def test_lifespan_no_tick_when_nothing_is_armed():
    """默认档:没有 scheduler、退避也没配 → 循环根本不启动(零负担)。"""
    import time as _time

    from trove.core.config import RetentionConfig

    actions = _FakeRetryActions(armed=False)
    app = create_app({
        "session_manager": object(),
        "connector_registry": _FakeRegistry(),
        "config": SimpleNamespace(
            scheduler_poll_seconds=1,
            retention=RetentionConfig(sweep_interval_hours=0)),
        "actions": actions,
    }, allow_null_auth=True)
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
        _time.sleep(0.3)
    assert actions.calls == 0


# ── 闭环验收测量腿(``trove.api.app._measure_outcomes``)────
#
# 挂在小时/天级的周期清扫上(测量的语义是 N 天,不占 30s 的 tick ——
# 与自动重试腿挂在 tick 上互为镜像)。默认关:outcome_after_days 没配时
# 循环都不为该腿启动;保留清理关着而测量开着时,循环按小时走。

class _FakeMeasureActions:
    """两腿都在一个对象上(测量 + 过期),整轮清扫不必换 state。"""

    def __init__(self, *, armed: bool = True) -> None:
        self.calls = 0
        self.expires = 0
        self.outcome_after_days = 7 if armed else 0

    async def measure_due(self) -> int:
        self.calls += 1
        return 2

    async def expire_due(self) -> int:
        self.expires += 1
        return 0


async def test_measure_outcomes_calls_the_service():
    from trove.api.app import _measure_outcomes

    actions = _FakeMeasureActions()
    await _measure_outcomes(_actions_app(actions))
    assert actions.calls == 1


async def test_measure_outcomes_skips_missing_layer():
    from trove.api.app import _measure_outcomes

    await _measure_outcomes(_actions_app(None))  # no-op, no exception


async def test_measure_outcomes_skips_when_not_armed():
    from trove.api.app import _measure_outcomes

    actions = _FakeMeasureActions(armed=False)
    await _measure_outcomes(_actions_app(actions))
    assert actions.calls == 0                    # 没配置 → 连服务都不叫


async def test_measure_outcomes_swallows_failures():
    from trove.api.app import _measure_outcomes

    class _BrokenActions:
        outcome_after_days = 7

        async def measure_due(self) -> int:
            raise RuntimeError("boom")

    await _measure_outcomes(_actions_app(_BrokenActions()))  # never raises


def test_measure_armed_follows_the_config():
    from trove.api.app import _measure_armed

    assert _measure_armed(_FakeMeasureActions(armed=True)) is True
    assert _measure_armed(_FakeMeasureActions(armed=False)) is False
    assert _measure_armed(None) is False
    assert _measure_armed(object()) is False     # 没有该字段的对象不炸


def test_lifespan_sweep_measures_when_retention_is_off(monkeypatch):
    """保留清理关着、测量开着 → 循环仍然按小时走测量腿。

    （老行为是 ``interval<=0`` 直接 return:测量腿会被"没开保留清理"
    顺带关掉 —— 两件不相干的事被一条条件绑在一起,正是这条测试钉的
    缝;maintenance 组件在生产里总是装配的,这里也必须给,否则测的
    是"循环压根没起"。）"""
    import asyncio

    from trove.core.config import RetentionConfig

    class _FakeMaintenance:
        def __init__(self):
            self.sweeps = 0

        async def run_all(self):
            self.sweeps += 1
            return {}

    real_sleep = asyncio.sleep

    async def short_sleep(delay: float) -> None:
        await real_sleep(0.05 if delay >= 3600 else delay)

    monkeypatch.setattr("trove.api.app.asyncio.sleep", short_sleep)
    actions = _FakeMeasureActions(armed=True)
    maintenance = _FakeMaintenance()
    app = create_app({
        "session_manager": object(),
        "connector_registry": _FakeRegistry(),
        "config": SimpleNamespace(
            retention=RetentionConfig(sweep_interval_hours=0)),
        "maintenance": maintenance,
        "actions": actions,
    }, allow_null_auth=True)
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
        _wait_until(lambda: actions.calls >= 1)
    assert actions.calls >= 1
    # 保留清理整段跳过:只有启动那一次(不在周期里反复跑)
    assert maintenance.sweeps == 1


def test_lifespan_periodic_sweep_measures_outcomes(monkeypatch):
    """保留清理与测量都开着:一轮完整清扫的末尾走到测量腿。"""
    import asyncio

    from trove.core.config import RetentionConfig

    class _FakeMaintenance:
        async def run_all(self):
            return {}

    real_sleep = asyncio.sleep

    async def short_sleep(delay: float) -> None:
        await real_sleep(0.05 if delay >= 3600 else delay)

    monkeypatch.setattr("trove.api.app.asyncio.sleep", short_sleep)
    actions = _FakeMeasureActions(armed=True)
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
        _wait_until(lambda: actions.calls >= 1)
    assert actions.calls >= 1
    assert actions.expires >= 1                  # 同一轮里两条腿都跑了


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
