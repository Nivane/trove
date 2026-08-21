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


def _components(maintenance=None, interval_hours=0) -> dict:
    """components dict in the create_app_components shape (config included)."""
    components = {
        "session_manager": object(),
        "connector_registry": object(),
        "config": SimpleNamespace(
            retention=RetentionConfig(sweep_interval_hours=interval_hours)
        ),
    }
    if maintenance is not None:
        components["maintenance"] = maintenance
    return components


def test_lifespan_startup_sweep_runs_and_exits_clean():
    """Shape A: maintenance + interval>0 -> startup sweep once, clean exit.

    The periodic loop sleeps interval*3600s, so it cannot fire inside the
    test window; the cancelled task must not hang or leak on shutdown
    ("Task was destroyed" must not appear in any output — covered by this
    test exiting without exception/warning).
    """
    maint = _FakeMaintenance()
    app = create_app(_components(maintenance=maint, interval_hours=24))
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
        # startup sweep has already run by the time the app is serving
        assert maint.calls == 1
    assert maint.calls == 1  # periodic never fired; no extra sweeps


def test_lifespan_without_maintenance_is_noop():
    """Shape B: api-test shape (no maintenance/config) -> zero side effects."""
    app = create_app({"session_manager": object(), "connector_registry": object()})
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200


def test_lifespan_interval_zero_startup_sweep_only():
    """Shape C: interval<=0 -> startup sweep still runs once, no periodic task."""
    maint = _FakeMaintenance()
    app = create_app(_components(maintenance=maint, interval_hours=0))
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
        assert maint.calls == 1
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
    app = create_app(_components(maintenance=maint, interval_hours=1))
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
        time.sleep(0.4)  # several periodic iterations at 0.05s/loop
    # startup sweep + at least one periodic sweep
    assert maint.calls >= 2
