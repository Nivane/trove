"""CoverageStore —— 迁移、活性唯一索引、心跳/中断、清退。

重点不是 CRUD 通不通,而是三件「数据上必须分开」的事:

- 「同源已有活跃 run」vs「真故障」→ 前者 409,后者上抛;
- 「跑完且零缺口」vs「没跑完」→ 后者 ``interrupted`` + 全题 skipped 行;
- 「最近 50 条」vs「更老的」→ 终态清退,活跃 run 永不清退。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trove.services.question_coverage.store import (
    ACTIVE_STATUSES,
    MAX_RUNS_PER_DATASOURCE,
    TERMINAL_STATUSES,
    CoverageRunActive,
    CoverageStore,
    now_iso,
    parse_iso,
)
from trove.storage.backends import build_backend

DS = "demo"


@pytest.fixture
async def store(tmp_path):
    s = CoverageStore(tmp_path, coverage_dir=tmp_path / "coverage")
    yield s
    await s.dispose()


def _iso(base: datetime | None = None, **delta) -> str:
    base = base or datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    return (base + timedelta(**delta)).isoformat()


def _run(run_id="r1", *, ds=DS, status="queued", questions=(("q1",), ("q2",)),
         created_at=None, **kw):
    return {
        "run_id": run_id,
        "datasource": ds,
        "status": status,
        "created_at": created_at or _iso(),
        "question_count": len(questions),
        "questions": [q[0] if isinstance(q, tuple) else q for q in questions],
        "taxonomy_version": 1,
        **kw,
    }


# ── 迁移 ────────────────────────────────────────────────


async def test_fresh_schema_and_reopen(store, tmp_path):
    await store.create_run(_run())
    assert store.db_path.exists()
    reopened = CoverageStore(tmp_path, coverage_dir=tmp_path / "coverage")
    try:
        got = await reopened.get_run("r1")
        assert got is not None and got["datasource"] == DS
    finally:
        await reopened.dispose()


async def test_legacy_tables_without_version_row_are_adopted(tmp_path):
    """存量库(有表、无版本记录)从 0 重放迁移 —— 逐条幂等,重放是空操作。"""
    db = tmp_path / "coverage" / "coverage.sqlite"
    db.parent.mkdir(parents=True)
    raw = build_backend(str(db))
    from trove.services.question_coverage.store import _migrations

    for op in _migrations()[0].ops:
        await raw.execute(op)
    await raw.commit()
    await raw.close()
    s = CoverageStore(tmp_path, coverage_dir=tmp_path / "coverage")
    try:
        await s.create_run(_run())
        assert (await s.get_run("r1"))["status"] == "queued"
    finally:
        await s.dispose()


async def test_too_new_schema_is_refused(tmp_path):
    from trove.storage.migrations import StorageSchemaTooNew

    db = tmp_path / "coverage" / "coverage.sqlite"
    db.parent.mkdir(parents=True)
    raw = build_backend(str(db))
    await raw.execute(
        "CREATE TABLE trove_schema (store TEXT PRIMARY KEY, version INTEGER"
        " NOT NULL, updated_at TEXT NOT NULL)")
    await raw.execute(
        "INSERT INTO trove_schema (store, version, updated_at)"
        " VALUES ('coverage', 99, 'x')")
    await raw.commit()
    await raw.close()
    s = CoverageStore(tmp_path, coverage_dir=tmp_path / "coverage")
    try:
        with pytest.raises(StorageSchemaTooNew):
            await s.list_runs(DS)
    finally:
        await s.dispose()


# ── 活性唯一索引 ────────────────────────────────────────


async def test_second_active_run_is_rejected_with_existing_id(store):
    await store.create_run(_run("r1"))
    with pytest.raises(CoverageRunActive) as exc:
        await store.create_run(_run("r2"))
    assert exc.value.run_id == "r1"


async def test_active_gate_reopens_after_terminal(store):
    await store.create_run(_run("r1"))
    await store.finish_run("r1", status="done", summary={"ok": 1})
    await store.create_run(_run("r2"))
    assert (await store.active_run(DS))["run_id"] == "r2"


async def test_active_gate_is_per_datasource(store):
    await store.create_run(_run("r1", ds="a"))
    await store.create_run(_run("r2", ds="b"))
    assert (await store.active_run("a"))["run_id"] == "r1"
    assert (await store.active_run("b"))["run_id"] == "r2"


async def test_active_status_list_matches_index_predicate(store):
    # 索引谓词里写死了 'queued','running' —— 常量一变,行为断言立刻红。
    assert ACTIVE_STATUSES == ("queued", "running")
    for status in TERMINAL_STATUSES:
        await store.create_run(_run(f"r-{status}"))
        await store.finish_run(f"r-{status}", status=status)
        await store.delete_run(f"r-{status}")


# ── 进度与结果 ──────────────────────────────────────────


async def test_add_result_updates_progress_and_counts(store):
    await store.create_run(_run(questions=("q1", "q2", "q3")))
    await store.mark_started("r1")
    await store.add_result("r1", {
        "seq": 0, "question": "q1", "outcome": "compiled",
        "intent": "query", "llm_calls": 1, "elapsed_ms": 12,
        "gaps": [], "detail": {"plan_typed": True},
    }, processed_count=1)
    await store.add_result("r1", {
        "seq": 1, "question": "q2", "outcome": "refused",
        "miss_class": "hard", "miss_reason": "fan_out", "gaps": [{"kind": "relation"}],
    }, processed_count=2)
    got = await store.get_run("r1")
    assert got["status"] == "running" and got["started_at"]
    assert got["processed_count"] == 2
    assert await store.outcome_counts("r1") == {"compiled": 1, "refused": 1}
    results = await store.get_results("r1")
    assert [r["seq"] for r in results] == [0, 1]
    assert results[1]["miss_reason"] == "fan_out"
    assert results[1]["gaps"] == [{"kind": "relation"}]


async def test_result_insert_is_idempotent_on_seq(store):
    await store.create_run(_run())
    row = {"seq": 0, "question": "q1", "outcome": "error"}
    await store.add_result("r1", row, processed_count=1)
    await store.add_result("r1", dict(row, outcome="compiled"), processed_count=1)
    results = await store.get_results("r1")
    assert len(results) == 1 and results[0]["outcome"] == "compiled"


async def test_get_results_seq_filter(store):
    await store.create_run(_run(questions=("a", "b", "c")))
    for seq in range(3):
        await store.add_result("r1", {
            "seq": seq, "question": "abc"[seq], "outcome": "compiled",
        }, processed_count=seq + 1)
    assert [r["seq"] for r in await store.get_results("r1", seqs=[2, 0])] == [0, 2]


# ── skipped 补齐 ────────────────────────────────────────


async def test_append_skipped_fills_only_missing_and_is_idempotent(store):
    await store.create_run(_run(questions=("a", "b", "c")))
    await store.add_result("r1", {
        "seq": 1, "question": "b", "outcome": "compiled"}, processed_count=1)
    added = await store.append_skipped("r1", [0, 1, 2], reason="cancelled")
    assert added == 2
    results = await store.get_results("r1")
    assert [(r["seq"], r["outcome"]) for r in results] == [
        (0, "skipped"), (1, "compiled"), (2, "skipped")]
    assert results[0]["outcome_reason"] == "cancelled"
    # 二次调用不再新增
    assert await store.append_skipped("r1", [0, 2], reason="cancelled") == 0
    assert (await store.get_run("r1"))["processed_count"] == 3


async def test_append_skipped_ignores_out_of_range_seqs(store):
    await store.create_run(_run(questions=("a",)))
    assert await store.append_skipped("r1", [0, 5], reason="interrupted") == 1


# ── 懒恢复 ──────────────────────────────────────────────


async def test_reconcile_stale_marks_interrupted_and_fills_skipped(store):
    now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    old = _iso(now, minutes=-30)
    await store.create_run(_run(created_at=old, heartbeat_at=old))
    await store.mark_started("r1", at=old)
    await store.add_result("r1", {
        "seq": 0, "question": "q1", "outcome": "compiled",
    }, processed_count=1, at=old)

    merged = await store.reconcile_stale(now=now)
    assert merged == ["r1"]
    got = await store.get_run("r1")
    assert got["status"] == "interrupted"
    assert got["finished_at"] and got["processed_count"] == 2
    results = await store.get_results("r1")
    assert [(r["seq"], r["outcome"], r["outcome_reason"]) for r in results] == [
        (0, "compiled", ""), (1, "skipped", "interrupted")]
    # 恢复后再建新 run 不被活性索引挡住
    await store.create_run(_run("r2", created_at=_iso(now)))


async def test_reconcile_stale_leaves_fresh_run_alone(store):
    now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    await store.create_run(_run(created_at=_iso(now, minutes=-1)))
    await store.mark_started("r1", at=_iso(now, minutes=-1))
    assert await store.reconcile_stale(now=now) == []
    assert (await store.get_run("r1"))["status"] == "running"


# ── 清退 ────────────────────────────────────────────────


async def test_purge_keeps_newest_terminal_and_never_active(store):
    base = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
    for i in range(MAX_RUNS_PER_DATASOURCE + 3):
        await store.create_run(_run(f"r{i:03d}", created_at=_iso(base, minutes=i)))
        await store.finish_run(f"r{i:03d}", status="done")
    await store.create_run(_run("active", created_at=_iso(base, minutes=-5)))
    await store.mark_started("active")

    purged = await store.purge_old_runs(DS)
    assert purged == 3
    remaining = {r["run_id"] for r in await store.list_runs(DS, limit=100)}
    assert "active" in remaining
    assert "r000" not in remaining and "r002" not in remaining
    assert "r003" in remaining
    # 被清退 run 的结果行一起删掉(没有孤儿)
    assert await store.get_results("r000") == []
    assert len(remaining) == MAX_RUNS_PER_DATASOURCE + 3 - 3 + 1


async def test_purge_is_per_datasource(store):
    for i in range(2):
        await store.create_run(_run(f"a{i}", ds="a"))
        await store.finish_run(f"a{i}", status="done")
        await store.create_run(_run(f"b{i}", ds="b"))
        await store.finish_run(f"b{i}", status="done")
    assert await store.purge_old_runs("a", keep=1) == 1
    assert len(await store.list_runs("b")) == 2


# ── 列表 / 基线 ─────────────────────────────────────────


async def test_list_runs_filters_and_orders(store):
    base = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
    await store.create_run(_run("r1", created_at=_iso(base, minutes=1)))
    await store.finish_run("r1", status="done")
    await store.create_run(_run("r2", created_at=_iso(base, minutes=2)))
    await store.finish_run("r2", status="error")
    await store.create_run(_run("r3", created_at=_iso(base, minutes=3)))
    rows = await store.list_runs(DS)
    assert [r["run_id"] for r in rows] == ["r3", "r2", "r1"]
    assert [r["run_id"] for r in await store.list_runs(DS, status="done")] == ["r1"]
    assert [r["run_id"] for r in await store.list_runs(DS, limit=1, offset=1)] == ["r2"]


async def test_previous_done_run(store):
    base = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
    await store.create_run(_run("old", created_at=_iso(base, minutes=1)))
    await store.finish_run("old", status="done")
    await store.create_run(_run("bad", created_at=_iso(base, minutes=2)))
    await store.finish_run("bad", status="error")
    await store.create_run(_run("cur", created_at=_iso(base, minutes=3)))
    prev = await store.previous_done_run(DS, _iso(base, minutes=3))
    assert prev["run_id"] == "old"


# ── 时间戳 ──────────────────────────────────────────────


def test_iso_timestamps_sort_lexicographically():
    a = "2026-10-18T12:00:00+00:00"          # 无微秒
    b = "2026-10-18T12:00:00.000001+00:00"   # 有微秒
    assert a < b
    assert parse_iso(a) < parse_iso(b)
    assert parse_iso("garbage") is None
    assert parse_iso("") is None
    assert parse_iso(now_iso()) is not None
