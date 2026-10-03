"""``VerdictStore`` — append-only history on the storage abstraction.

Runs on the SQLite backend against a tmp path (zero network), like every
other internal store. **Every fixture disposes**: aiosqlite's worker thread
is non-daemon, so a leaked connection makes pytest hang after the last test
rather than fail one.
"""

from __future__ import annotations

import pytest

from trove.services.decision.verdict_store import VerdictStore
from trove.services.decision.verdicts import VerdictRecord


def verdict(**overrides) -> VerdictRecord:
    base = dict(
        datasource="demo", rule_id="loan-drop", status="alert", triggered=True,
        rule_digest="d1", run_id=1, job_id="j1", severity="critical",
        priority=2, message="贷款余额环比下滑", row_count=3,
        evidence={"rows": [{"dim": "华东", "delta_pct": -0.2, "triggered": True}],
                  "evidence": {"row_count": 3}},
        anchor_date="2026-10-01", evaluated_at="2026-10-01T09:00:00",
        created_at="2026-10-01T09:00:00",
    )
    base.update(overrides)
    return VerdictRecord(**base)


@pytest.fixture
async def store(tmp_path):
    s = VerdictStore(tmp_path)
    try:
        yield s
    finally:
        await s.dispose()


class TestRecordAndRead:
    async def test_round_trip(self, store):
        vid = await store.record(verdict())
        assert vid > 0
        got = await store.get(vid)
        assert got is not None
        assert got.rule_id == "loan-drop" and got.priority == 2
        assert got.triggered is True and got.status == "alert"
        assert got.evidence["rows"][0]["dim"] == "华东"
        assert got.rule_digest == "d1" and got.run_id == 1

    async def test_get_missing_is_none(self, store):
        assert await store.get(9999) is None

    async def test_list_is_newest_first_and_scoped(self, store):
        await store.record(verdict(evaluated_at="2026-10-01T09:00:00"))
        await store.record(verdict(evaluated_at="2026-10-02T09:00:00"))
        await store.record(verdict(rule_id="other", evaluated_at="2026-10-03T09:00:00"))
        rows = await store.list_for_rule("demo", "loan-drop")
        assert [r.evaluated_at for r in rows] == [
            "2026-10-02T09:00:00", "2026-10-01T09:00:00"]

    async def test_list_limit_and_since(self, store):
        for day in ("01", "02", "03"):
            await store.record(verdict(evaluated_at=f"2026-10-{day}T09:00:00"))
        assert len(await store.list_for_rule("demo", "loan-drop", limit=2)) == 2
        since = await store.list_for_rule(
            "demo", "loan-drop", since="2026-10-02T00:00:00")
        assert [r.evaluated_at[:10] for r in since] == ["2026-10-03", "2026-10-02"]

    async def test_latest_for_rules(self, store):
        await store.record(verdict(evaluated_at="2026-10-01T09:00:00"))
        await store.record(verdict(evaluated_at="2026-10-02T09:00:00"))
        out = await store.latest_for_rules("demo", ["loan-drop", "nope"])
        assert set(out) == {"loan-drop"}
        assert out["loan-drop"].evaluated_at == "2026-10-02T09:00:00"

    async def test_same_day_runs_order_by_id(self, store):
        """A daily rule re-run by hand lands two rows on one timestamp; the
        tiebreak has to be the insertion order, not chance."""
        first = await store.record(
            verdict(evaluated_at="2026-10-02T09:00:00", message="first"))
        second = await store.record(
            verdict(evaluated_at="2026-10-02T09:00:00", message="second"))
        assert second > first
        rows = await store.list_for_rule("demo", "loan-drop")
        assert [r.message for r in rows] == ["second", "first"]

    async def test_previous_for_is_strictly_before(self, store):
        v1 = await store.record(verdict(evaluated_at="2026-10-01T09:00:00"))
        await store.record(verdict(evaluated_at="2026-10-02T09:00:00"))
        cur = await store.get(await store.record(
            verdict(evaluated_at="2026-10-03T09:00:00")))
        prev = await store.previous_for("demo", "loan-drop", cur)
        assert prev is not None and prev.evaluated_at == "2026-10-02T09:00:00"

        first = await store.get(v1)
        assert await store.previous_for("demo", "loan-drop", first) is None

    async def test_previous_for_same_timestamp_uses_id(self, store):
        first_id = await store.record(verdict(evaluated_at="2026-10-02T09:00:00"))
        await store.record(verdict(evaluated_at="2026-10-02T09:00:00"))
        second = await store.get(
            await store.record(verdict(evaluated_at="2026-10-02T09:00:00")))
        prev = await store.previous_for("demo", "loan-drop", second)
        assert prev is not None and prev.id != first_id
        assert prev.evaluated_at == "2026-10-02T09:00:00"

    async def test_unicode_evidence_survives(self, store):
        vid = await store.record(verdict(
            message="华东区贷款余额跌破阈值——建议复核",
            evidence={"rows": [{"dim": "西南", "note": "环比-12.3%"}]}))
        got = await store.get(vid)
        assert got.message.startswith("华东区")
        assert got.evidence["rows"][0]["dim"] == "西南"


class TestRetention:
    async def test_purge_before_cutoff(self, store):
        await store.record(verdict(evaluated_at="2026-09-01T09:00:00"))
        await store.record(verdict(evaluated_at="2026-10-01T09:00:00"))
        deleted = await store.purge_before("2026-09-15T00:00:00")
        assert deleted == 1
        rows = await store.list_for_rule("demo", "loan-drop")
        assert [r.evaluated_at[:10] for r in rows] == ["2026-10-01"]

    async def test_purge_before_keeps_boundary(self, store):
        await store.record(verdict(evaluated_at="2026-09-15T00:00:00"))
        assert await store.purge_before("2026-09-15T00:00:00") == 0


class TestSchema:
    async def test_migrations_are_idempotent(self, tmp_path):
        """Re-opening the same path replays every step; each is idempotent,
        so a second open is a no-op rather than an error."""
        first = VerdictStore(tmp_path)
        try:
            await first.record(verdict())
            vid = await first.record(verdict(evaluated_at="2026-10-02T09:00:00"))
        finally:
            await first.dispose()
        second = VerdictStore(tmp_path)
        try:
            assert await second.get(vid) is not None
            assert len(await second.list_for_rule("demo", "loan-drop")) == 2
        finally:
            await second.dispose()

    async def test_database_newer_than_code_is_refused(self, tmp_path):
        from trove.storage.backends import resolve_backend
        from trove.storage.migrations import SQLITE, ensure_version_table, write_version

        db = tmp_path / ".trove" / "decisions" / "verdicts.sqlite"
        db.parent.mkdir(parents=True)
        backend = resolve_backend(str(db))
        try:
            await ensure_version_table(backend, dialect=SQLITE)
            await write_version(backend, "decision_verdicts", 99, dialect=SQLITE)
            await backend.commit()
        finally:
            await backend.dispose()

        store = VerdictStore(tmp_path)
        try:
            with pytest.raises(Exception) as excinfo:
                await store.list_for_rule("demo", "loan-drop")
            assert "99" in str(excinfo.value)
        finally:
            await store.dispose()
