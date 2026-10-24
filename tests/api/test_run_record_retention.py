"""Periodic-sweep retention leg for run records.

The switch does not gate cleanup: after capture is turned off, old evidence
still ages out. ``days <= 0`` means permanent. A store that was never used
must not be materialized just to discover it is empty, and a failed purge
must not take the other sweep legs down with it.
"""

from __future__ import annotations

from types import SimpleNamespace

from trove.api.app import _purge_run_records
from trove.storage.run_record_store import DB_FILE_NAME, RunRecordStore


def _app(store, days: int = 30):
    return SimpleNamespace(state=SimpleNamespace(
        run_records=store,
        config=SimpleNamespace(
            retention=SimpleNamespace(run_record_retention_days=days)),
    ))


def _header(run_id: str, started_at: str) -> dict:
    return {
        "run_id": run_id, "session_id": "s", "question": "q",
        "status": "done", "started_at": started_at,
    }


def _events(seq: int):
    return [{"seq": seq, "kind": "run", "payload": {}}]


async def _seed(store: RunRecordStore) -> None:
    await store.write(
        _header("old", "2020-01-01T00:00:00+00:00"), _events(0))
    await store.write(
        _header("new", "2099-01-01T00:00:00+00:00"), _events(0))


async def test_old_runs_and_events_are_purged(tmp_path):
    store = RunRecordStore(tmp_path)
    try:
        await _seed(store)
        await _purge_run_records(_app(store))
        assert await store.get("old") is None
        assert await store.get("new") is not None
        conn = await store._conn()
        try:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM run_record_events")
            row = await cursor.fetchone()
            assert int(row[0]) == 1  # no orphan events
        finally:
            await conn.close()
    finally:
        await store.dispose()


async def test_days_zero_keeps_everything(tmp_path):
    store = RunRecordStore(tmp_path)
    try:
        await _seed(store)
        await _purge_run_records(_app(store, days=0))
        assert await store.get("old") is not None
        assert await store.get("new") is not None
    finally:
        await store.dispose()


async def test_unused_store_is_not_materialized(tmp_path):
    store = RunRecordStore(tmp_path)
    try:
        await _purge_run_records(_app(store))
        assert not (tmp_path / DB_FILE_NAME).exists()
    finally:
        await store.dispose()


async def test_missing_store_or_config_is_a_noop(tmp_path):
    await _purge_run_records(SimpleNamespace(state=SimpleNamespace()))
    await _purge_run_records(SimpleNamespace(
        state=SimpleNamespace(run_records=None)))


async def test_purge_failure_is_swallowed(tmp_path):
    store = RunRecordStore(tmp_path)

    async def boom(_cutoff):
        raise RuntimeError("db locked")

    try:
        await _seed(store)
        store.purge_before = boom
        await _purge_run_records(_app(store))  # never raises
    finally:
        await store.dispose()
