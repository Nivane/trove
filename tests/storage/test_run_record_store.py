"""``RunRecordStore`` — durable run headers + append-only events.

Covered: round-trip, explicit-seq idempotence (a retried flush must not
invent new rows), header upsert semantics (``started_at``/``created_at`` are
history, not "latest flush"), retention with event cascade, and
``has_data`` never materializing an empty database.
"""

from __future__ import annotations

import os

import pytest

from trove.storage.run_record_store import DB_FILE_NAME, RunRecordStore


def _header(run_id: str, **over) -> dict:
    base = {
        "run_id": run_id,
        "session_id": "s1",
        "user_id": "u1",
        "question": "q?",
        "workflow": "reflection",
        "datasource": "test_db",
        "status": "running",
        "started_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(over)
    return base


def _event(seq: int, kind: str = "tool", **payload) -> dict:
    return {"seq": seq, "kind": kind, "payload": payload or {"x": seq}}


class TestRoundTrip:
    async def test_write_then_get(self, tmp_path):
        store = RunRecordStore(tmp_path)
        try:
            n = await store.write(
                _header("r1", kb_rev="abc", status="done",
                        finish_json={"verdict": "OK"}, truncated=1,
                        truncation_json={"events_dropped": 2}),
                [
                    _event(0, "run", question="q?"),
                    _event(1, "llm", output="SELECT 1"),
                    _event(2, "tool", observation="raw rows"),
                ],
            )
            assert n == 3
            record = await store.get("r1")
            assert record is not None
            header = record["header"]
            assert header["status"] == "done"
            assert header["kb_rev"] == "abc"
            assert header["finish"] == {"verdict": "OK"}
            assert header["truncated"] is True
            assert header["truncation"] == {"events_dropped": 2}
            assert [e["seq"] for e in record["events"]] == [0, 1, 2]
            assert record["events"][2]["payload"]["observation"] == "raw rows"
            assert record["events"][0]["kind"] == "run"
        finally:
            await store.dispose()

    async def test_unknown_run_is_none(self, tmp_path):
        store = RunRecordStore(tmp_path)
        try:
            assert await store.get("nope") is None
        finally:
            await store.dispose()

    async def test_header_upsert_keeps_start_times(self, tmp_path):
        """status/finish 覆盖,started_at/created_at 不随后续 flush 漂移。"""
        store = RunRecordStore(tmp_path)
        try:
            await store.write(_header("r1"), [])
            record = await store.get("r1")
            started_at = record["header"]["started_at"]
            created_at = record["header"]["created_at"]

            await store.write(
                _header("r1", started_at="2099-01-01T00:00:00+00:00",
                        status="done"),
                [],
            )
            record = await store.get("r1")
            assert record["header"]["status"] == "done"
            assert record["header"]["started_at"] == started_at
            assert record["header"]["created_at"] == created_at
        finally:
            await store.dispose()


class TestIdempotentWrites:
    async def test_retried_flush_does_not_duplicate(self, tmp_path):
        store = RunRecordStore(tmp_path)
        try:
            header = _header("r1")
            events = [_event(0, "run"), _event(1, "tool"), _event(2, "llm")]
            await store.write(header, events)
            await store.write(header, events)  # same snapshot again
            record = await store.get("r1")
            assert [e["seq"] for e in record["events"]] == [0, 1, 2]
        finally:
            await store.dispose()

    async def test_second_flush_appends_after_first(self, tmp_path):
        store = RunRecordStore(tmp_path)
        try:
            await store.write(_header("r1"), [_event(0), _event(1)])
            await store.write(_header("r1"), [_event(1), _event(2), _event(3)])
            record = await store.get("r1")
            assert [e["seq"] for e in record["events"]] == [0, 1, 2, 3]
        finally:
            await store.dispose()

    async def test_events_without_seq_append_after_current_max(self, tmp_path):
        store = RunRecordStore(tmp_path)
        try:
            await store.write(_header("r1"), [_event(0), _event(1)])
            await store.write(_header("r1"), [
                {"kind": "tool", "payload": {"a": 1}},
                {"kind": "tool", "payload": {"a": 2}},
            ])
            record = await store.get("r1")
            assert [e["seq"] for e in record["events"]] == [0, 1, 2, 3]
        finally:
            await store.dispose()


class TestRetention:
    async def test_purge_removes_run_and_events_together(self, tmp_path):
        store = RunRecordStore(tmp_path)
        try:
            await store.write(
                _header("old", started_at="2020-01-01T00:00:00+00:00"),
                [_event(0), _event(1)],
            )
            await store.write(
                _header("new", started_at="2099-01-01T00:00:00+00:00"),
                [_event(0)],
            )
            counts = await store.count_before("2030-01-01T00:00:00+00:00")
            assert counts == {"runs": 1, "events": 2}

            purged = await store.purge_before("2030-01-01T00:00:00+00:00")
            assert purged == {"runs": 1, "events": 2}
            assert await store.get("old") is None
            assert (await store.get("new")) is not None

            # No orphan events survive the purge (a later purge could never see them).
            conn = await store._conn()
            try:
                cursor = await conn.execute(
                    "SELECT COUNT(*) FROM run_record_events")
                row = await cursor.fetchone()
                assert int(row[0]) == 1
            finally:
                await conn.close()
        finally:
            await store.dispose()

    async def test_purge_with_nothing_old_is_a_noop(self, tmp_path):
        store = RunRecordStore(tmp_path)
        try:
            await store.write(
                _header("new", started_at="2099-01-01T00:00:00+00:00"),
                [_event(0)],
            )
            assert await store.purge_before("2030-01-01T00:00:00+00:00") == {
                "runs": 0, "events": 0}
            assert (await store.get("new")) is not None
        finally:
            await store.dispose()


class TestHasData:
    async def test_missing_file_is_false_and_stays_missing(self, tmp_path):
        store = RunRecordStore(tmp_path)
        try:
            assert await store.has_data() is False
            # The retention leg must not materialize an empty DB just to
            # discover there is nothing to purge.
            assert not (tmp_path / DB_FILE_NAME).exists()
        finally:
            await store.dispose()

    async def test_existing_file_with_rows_is_true(self, tmp_path):
        store = RunRecordStore(tmp_path)
        try:
            await store.write(_header("r1"), [])
            assert await store.has_data() is True
        finally:
            await store.dispose()


PG_URL = os.environ.get("PG_TEST_URL")


@pytest.mark.integration
async def test_run_records_round_trip_on_pg(monkeypatch, tmp_path):
    """Same store contract on the production backend (env-gated)."""
    if not PG_URL:
        pytest.skip("PG_TEST_URL not set")
    monkeypatch.setenv("TROVE_STORAGE_URL", PG_URL)
    store = RunRecordStore(tmp_path)
    run_id = f"pg-test-{os.getpid()}-{tmp_path.name}"
    try:
        await store.write(
            _header(run_id, status="done", finish_json={"sql": "SELECT 1"}),
            [_event(0, "run"), _event(1, "llm", output="hi")],
        )
        record = await store.get(run_id)
        assert record["header"]["status"] == "done"
        assert record["header"]["finish"] == {"sql": "SELECT 1"}
        assert [e["seq"] for e in record["events"]] == [0, 1]
        await store.write(_header(run_id), [_event(0, "run"), _event(1, "llm")])
        assert len((await store.get(run_id))["events"]) == 2
    finally:
        # Shared CI database: only rows this test created are purged (by id).
        try:
            conn = await store._conn()
            for table in ("run_record_events", "run_records"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
            await conn.commit()
            await conn.close()
        finally:
            await store.dispose()
