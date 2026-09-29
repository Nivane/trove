"""Lifespan smoke tests: maintenance sweep wiring on app startup/shutdown.

The api suite's ASGITransport does not trigger lifespan events, so the
startup/periodic sweep paths in trove.api.app._lifespan / _periodic_sweep
are only exercised here via starlette's TestClient (which runs lifespan
on enter/exit).

These are SYNC tests on purpose: TestClient brings its own event loop
(portal thread) — do not mark them pytest.mark.asyncio.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from trove.api.app import create_app
from trove.core.config import RetentionConfig


class _FakeMaintenance:
    """run_all counts calls; enough to observe the sweep wiring."""

    def __init__(self) -> None:
        self.calls = 0

    async def run_all(self):
        self.calls += 1
        return {"orphans": 0, "pruned": 0, "sweep": "scanned=0"}


class _FakeRegistry:
    """Satisfies the /v1/health datasource probe (list_names → no pings)."""

    def list_names(self) -> list[str]:
        return []


def _components(maintenance=None, interval_hours=0) -> dict:
    """components dict in the create_app_components shape (config included)."""
    components = {
        "session_manager": object(),
        "connector_registry": _FakeRegistry(),
        "config": SimpleNamespace(
            retention=RetentionConfig(sweep_interval_hours=interval_hours)
        ),
    }
    if maintenance is not None:
        components["maintenance"] = maintenance
    return components


def _wait_until(predicate, timeout_s: float = 0.5) -> None:
    """Poll a condition while the portal thread's loop runs (sync context).

    Startup sweep now runs as a background task (non-blocking), so
    assertions about it must poll instead of assuming it ran synchronously.
    """
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not reached within {timeout_s:.1f}s")
        time.sleep(0.01)


def test_lifespan_startup_sweep_runs_and_exits_clean():
    """Shape A: maintenance + interval>0 -> startup sweep once, clean exit.

    Startup sweep runs as a background task (does not block serve); the
    periodic loop sleeps interval*3600s, so it cannot fire inside the
    test window; the cancelled tasks must not hang or leak on shutdown
    ("Task was destroyed" must not appear in any output — covered by this
    test exiting without exception/warning).
    """
    maint = _FakeMaintenance()
    app = create_app(_components(maintenance=maint, interval_hours=24), allow_null_auth=True)
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
        _wait_until(lambda: maint.calls >= 1)  # startup sweep runs in background
    assert maint.calls == 1  # periodic never fired; no extra sweeps


def test_lifespan_without_maintenance_is_noop():
    """Shape B: api-test shape (no maintenance/config) -> zero side effects."""
    app = create_app(
        {"session_manager": object(), "connector_registry": _FakeRegistry()},
        allow_null_auth=True,
    )
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200


def test_lifespan_runs_readonly_selfcheck(caplog):
    """启动自检(设计 §4 I1):每个源问一遍账号能不能写,结果挂在 app.state。

    这条自检**同步等**(不像 sweep 那样丢后台):后台跑的话进程会先开始收
    请求,那几秒 health 只能报 ``not_probed``,可它不是「没探过」是「正在
    探」—— 同一个 basis 说两件事,就又是一种谎。代价是启动多等一个往返
    (单源 3s 有界、并发),而这个往返换来的是:进程一开始收请求,答案就在。
    """
    from trove.core.types import BASIS_GRANTS, ReadonlyProbe

    class _WritableAdapter:
        name = "shop"
        is_connected = True

        async def execute(self, sql):
            return None  # health 的 SELECT 1 探针:这个源本身是好的

        async def probe_readonly(self):
            # detail 带上授权原文:它只该进日志,不该进响应(health 免鉴权)
            return ReadonlyProbe(False, BASIS_GRANTS, "GRANT INSERT ON `shop`.*")

    class _ProbeRegistry(_FakeRegistry):
        def __init__(self):
            self._adapters = {"shop": _WritableAdapter()}

        def list_names(self):
            return list(self._adapters)

        async def get(self, name):
            return self._adapters[name]

    components = _components()
    components["connector_registry"] = _ProbeRegistry()
    app = create_app(components, allow_null_auth=True)
    with TestClient(app) as c:
        # 进到这里意味着 lifespan 的 startup 段已经跑完 —— 自检是同步等的,
        # 所以不必轮询:答不出结论就不该开始服务。
        result = app.state.readonly_probes["shop"]
        assert result.verified is False
        # 钉**级别**而不只是文案:caplog.text 把 INFO 也收进来,只断言文案的话
        # 「这条降级成 INFO」这个变异会活下来(就是不显眼了,而它恰恰要显眼)。
        marked = [
            r for r in caplog.records
            if r.name == "trove.api.app" and "verified=false" in r.getMessage()
        ]
        assert marked, "自检结论没进日志"
        assert marked[0].levelno >= logging.WARNING
        # 结论进 health,但 detail 不进(GRANT 原文里有库名与账号名)
        resp = c.get("/v1/health")
        entry = resp.json()["checks"]["datasources"]["shop"]
        assert entry["readonly"] == {"verified": False, "basis": "grants"}
        assert "GRANT" not in resp.text


def test_create_app_without_auth_fails_closed():
    """缺 auth 组件默认拒绝启动(不再静默降级为 local admin)。"""
    import pytest

    with pytest.raises(RuntimeError, match="auth"):
        create_app({"session_manager": object(), "connector_registry": _FakeRegistry()})
    # 显式 opt-in 才走 NullAuth 本地管理员兜底
    app = create_app(
        {"session_manager": object(), "connector_registry": _FakeRegistry()},
        allow_null_auth=True,
    )
    assert app.state.auth is not None


def test_lifespan_interval_zero_startup_sweep_only():
    """Shape C: interval<=0 -> startup sweep still runs once, no periodic task."""
    maint = _FakeMaintenance()
    app = create_app(_components(maintenance=maint, interval_hours=0), allow_null_auth=True)
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
        _wait_until(lambda: maint.calls >= 1)
    assert maint.calls == 1


def test_lifespan_periodic_sweep_fires(monkeypatch):
    """Periodic path: shorten the hour-scale sleep so the loop can fire.

    `trove.api.app.asyncio` IS the asyncio module, so this patch is
    process-global while the portal thread runs — the wrapper falls back
    to the real sleep for non-hour delays, so nothing else is affected,
    and monkeypatch restores it at teardown.
    """
    maint = _FakeMaintenance()
    real_sleep = asyncio.sleep

    async def short_sleep(delay: float) -> None:
        await real_sleep(0.05 if delay >= 3600 else delay)

    monkeypatch.setattr("trove.api.app.asyncio.sleep", short_sleep)
    app = create_app(_components(maintenance=maint, interval_hours=1), allow_null_auth=True)
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
        time.sleep(0.4)  # several periodic iterations at 0.05s/loop
    # startup sweep + at least one periodic sweep
    assert maint.calls >= 2


def test_lifespan_startup_sweep_does_not_block_serve():
    """启动 sweep 不得阻塞 serve 就绪(spec: 不阻塞)。

    A regression to `await maintenance.run_all()` inline in _lifespan
    would block the portal thread — the health request could not be
    serviced (TestClient enter or the request would hang past the
    httpx/portal timeout). This test is throw-shaped against that.
    """

    class _BlockingMaintenance:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run_all(self):
            self.started.set()
            await self.release.wait()  # stay in-flight until released
            return {"orphans": 0, "pruned": 0, "sweep": "scanned=0"}

    maint = _BlockingMaintenance()
    app = create_app(_components(maintenance=maint, interval_hours=0), allow_null_auth=True)
    with TestClient(app) as c:
        _wait_until(lambda: maint.started.is_set())  # sweep task started...
        # ...and serve must answer while the sweep is still blocked in-flight
        assert c.get("/v1/health").status_code == 200
        maint.release.set()


def test_lifespan_job_tick_fires():
    """Scheduler tick: shortened poll interval lets the background loop run.

    The tick task exits immediately when no scheduler is wired in, so the
    api-suite shape (no scheduler component) never starts a loop; this test
    wires a fake scheduler and confirms due jobs are picked up.
    """

    class _FakeJobs:
        def __init__(self) -> None:
            self.ticks = 0

        async def tick(self):
            self.ticks += 1
            return []

    components = _components()
    components["scheduler"] = _FakeJobs()
    components["config"] = SimpleNamespace(
        scheduler_poll_seconds=1,
        retention=RetentionConfig(sweep_interval_hours=0),
    )
    app = create_app(components, allow_null_auth=True)
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
        # tick 循环先 sleep(poll) 再执行;轮询等待首个 tick(最长 3s)。
        _wait_until(lambda: components["scheduler"].ticks >= 1, timeout_s=3.0)
    assert components["scheduler"].ticks >= 1


def test_lifespan_job_tick_disabled_when_poll_zero():
    """scheduler_poll_seconds <= 0 → tick loop never starts (no side effects)."""

    class _FakeJobs:
        def __init__(self) -> None:
            self.ticks = 0

        async def tick(self):
            self.ticks += 1
            return []

    components = _components()
    components["scheduler"] = _FakeJobs()
    components["config"] = SimpleNamespace(
        scheduler_poll_seconds=0,
        retention=RetentionConfig(sweep_interval_hours=0),
    )
    app = create_app(components, allow_null_auth=True)
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
        time.sleep(0.3)
    assert components["scheduler"].ticks == 0
