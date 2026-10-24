"""``_dispose_components`` — drain first, run-records store last, failures isolated.

Order is load-bearing twice over: the recorder's flush may still be writing
while other components close, so the drain runs before any dispose; and the
run-record store is closed only after every other component has finished.
Each step is isolated — one broken store must not leave the rest holding the
process (aiosqlite workers are non-daemon).
"""

from __future__ import annotations

import types

import trove.tracing.run_record as run_record
from trove.main import DISPOSE_PLAN, _dispose_components, _resolve_method


class Recorder:
    """Exposes all three disposal shapes the plan uses."""

    def __init__(self, name, order, *, fail=False):
        self.name = name
        self.order = order
        self.fail = fail
        self.dispose = self._dispose
        self.close_all = self._dispose
        self.store = types.SimpleNamespace(dispose=self._dispose)

    async def _dispose(self):
        self.order.append(self.name)
        if self.fail:
            raise RuntimeError(f"{self.name} down")


def _components(order, *, failing=frozenset()):
    comps = {}
    for name, _method in DISPOSE_PLAN:
        comps[name] = Recorder(name, order, fail=name in failing)
    return comps


async def test_drain_runs_first_and_store_closes_last(monkeypatch):
    order: list[str] = []

    async def drain():
        order.append("drain")

    monkeypatch.setattr(run_record, "drain_run_records", drain)
    await _dispose_components(_components(order))

    assert order[0] == "drain"
    assert order[-1] == "run_records"
    assert set(order[1:]) == {name for name, _ in DISPOSE_PLAN}


async def test_drain_failure_does_not_skip_disposals(monkeypatch):
    order: list[str] = []

    async def drain():
        order.append("drain")
        raise RuntimeError("recorder stuck")

    monkeypatch.setattr(run_record, "drain_run_records", drain)
    await _dispose_components(_components(order))

    assert order[0] == "drain"
    assert order[-1] == "run_records"
    assert len(order) == len(DISPOSE_PLAN) + 1


async def test_one_failing_component_does_not_stop_the_rest(monkeypatch):
    order: list[str] = []

    async def drain():
        order.append("drain")

    monkeypatch.setattr(run_record, "drain_run_records", drain)
    await _dispose_components(_components(order, failing={"jobs", "memory"}))

    disposed = order[1:]
    assert disposed[-1] == "run_records"
    assert "jobs" in disposed and "memory" in disposed
    assert set(disposed) == {name for name, _ in DISPOSE_PLAN}


async def test_missing_and_non_callable_entries_are_skipped(monkeypatch):
    order: list[str] = []

    async def drain():
        order.append("drain")

    monkeypatch.setattr(run_record, "drain_run_records", drain)
    components = {
        "auth": Recorder("auth", order),
        "run_records": types.SimpleNamespace(dispose=None),
        "settings": None,
    }
    await _dispose_components(components)
    assert order == ["drain", "auth"]


def test_resolve_method_supports_dotted_paths():
    obj = types.SimpleNamespace(store=types.SimpleNamespace(dispose=lambda: 1))
    assert _resolve_method(obj, "store.dispose") is not None
    assert _resolve_method(obj, "store.nope") is None
    assert _resolve_method(None, "dispose") is None
    assert _resolve_method(types.SimpleNamespace(dispose=1), "dispose") is None
