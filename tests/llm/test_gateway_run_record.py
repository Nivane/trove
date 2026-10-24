"""The gateway's two capture points: local LLM calls and failed generations.

Most pipeline call sites do not stamp ``metadata.run_id``; the session sets
``run_id_var`` for the whole run so those calls still land in the same record
(and, as a side effect, in the per-run token accumulator). Failure capture
must happen before the Langfuse early return, otherwise a run with no
observability client has no record of the call that failed.
"""

from __future__ import annotations

import pytest

from trove.core.request_id import run_id_var
from trove.llm.gateway import _record_failed_generation, _record_local_call
from trove.llm.token_accounting import pop
from trove.tracing.run_record import (
    configure_run_records,
    drain_run_records,
    get_run_recorder,
    reset_run_records,
)


class FakeStore:
    def __init__(self):
        self.writes: list[tuple[dict, list[dict]]] = []

    async def write(self, header, events):
        self.writes.append((dict(header), [dict(e) for e in events]))
        return len(events)


@pytest.fixture
async def store():
    fake = FakeStore()
    configure_run_records(fake, enabled=True)
    rec = get_run_recorder()
    assert rec is not None
    # Open a run buffer so captured events have somewhere to accumulate.
    rec.capture_run_start("r-ctx", question="q?")
    try:
        yield fake
    finally:
        await drain_run_records()
        reset_run_records()


def _events(fake: FakeStore) -> list[dict]:
    assert fake.writes, "nothing was flushed"
    return fake.writes[-1][1]


async def _force_flush(run_id: str) -> None:
    """Force one flush — captures other than lifecycle points don't schedule."""
    rec = get_run_recorder()
    assert rec is not None
    rec.schedule_flush(run_id)
    await drain_run_records()


async def test_local_call_falls_back_to_run_id_var(store):
    run_id_var.set("r-ctx")
    _record_local_call(
        "mock/model", [{"role": "user", "content": "hi"}], "SELECT 1",
        None, 12, temperature=0.0, reasoning="",
        usage={"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
    )
    await _force_flush("r-ctx")

    events = _events(store)
    llm = next(e for e in events if e["kind"] == "llm")
    assert llm["node"] == ""
    assert llm["payload"]["model"] == "mock/model"
    assert llm["payload"]["output"] == "SELECT 1"
    assert llm["payload"]["messages"] == [{"role": "user", "content": "hi"}]
    assert llm["payload"]["usage"]["total_tokens"] == 12
    # The same fallback finally accounts these calls per run.
    assert pop("r-ctx") == {"prompt": 5, "completion": 7, "total": 12}


async def test_metadata_run_id_wins_over_var(store):
    run_id_var.set("r-ctx")
    _record_local_call(
        "mock/model", [{"role": "user", "content": "hi"}], "out",
        {"run_id": "r-meta", "node": "gen_generate"}, 3,
    )
    await _force_flush("r-meta")

    assert store.writes[-1][0]["run_id"] == "r-meta"
    llm = next(e for e in _events(store) if e["kind"] == "llm")
    assert llm["node"] == "gen_generate"


async def test_no_run_id_means_silence(store):
    _record_local_call("m", [], "out", None, 1,
                       usage={"total_tokens": 9})
    await _force_flush("r-ctx")
    # Only the fixture's run_start event — the library call stayed silent.
    assert [e["kind"] for e in _events(store)] == ["run"]
    assert pop("r-ctx") is None


async def test_capture_failure_does_not_block_token_accounting(
        monkeypatch, store):
    run_id_var.set("r-ctx")

    def boom(*a, **k):
        raise RuntimeError("recorder down")

    monkeypatch.setattr("trove.tracing.run_record.capture_llm", boom)
    _record_local_call("m", [], "out", None, 1,
                       usage={"total_tokens": 9})
    assert pop("r-ctx") == {"prompt": 0, "completion": 0, "total": 9}


async def test_failed_generation_recorded_before_langfuse_early_return(
        monkeypatch, store):
    monkeypatch.setattr("trove.llm.gateway.get_client", lambda: None)
    run_id_var.set("r-ctx")
    _record_failed_generation(
        "mock/model", [{"role": "user", "content": "hi"}],
        RuntimeError("upstream 503"), None,
    )
    await _force_flush("r-ctx")

    events = _events(store)
    err = next(e for e in events if e["kind"] == "llm_error")
    assert err["payload"]["model"] == "mock/model"
    assert "upstream 503" in err["payload"]["error"]


async def test_failed_generation_metadata_run_id_wins(monkeypatch, store):
    monkeypatch.setattr("trove.llm.gateway.get_client", lambda: None)
    run_id_var.set("r-ctx")
    _record_failed_generation(
        "m", [], "boom", {"run_id": "r-meta", "node": "gen_generate"})
    await _force_flush("r-meta")

    assert store.writes[-1][0]["run_id"] == "r-meta"
    err = next(e for e in _events(store) if e["kind"] == "llm_error")
    assert err["node"] == "gen_generate"
