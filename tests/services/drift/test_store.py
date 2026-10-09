"""DriftStore —— 持久化、幂等(I5)、重开语义、以及 skipped 不碰条目(I3)。

这一层的存在理由就是「报完即忘」的缺陷:同名漂移每次都像新的。所以测试的
重点不是 CRUD 通不通,而是**两类事件能不能在数据上分开**:

- 「这条昨天就有」vs「这条刚冒出来」→ ``seen_count`` / ``new_count``
- 「查过且干净」vs「没查成」      → ``drift_run.status`` + 条目时间戳不动
"""

from __future__ import annotations

import pytest

from trove.services.drift.models import (
    L1,
    L2,
    RUN_OK,
    RUN_SKIPPED,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    SOURCE_EXTERNAL,
    STATUS_OPEN,
    STATUS_RESOLVED,
    STATUS_WAIVED,
    DriftItem,
    DriftReport,
)
from trove.services.drift.store import DriftStore

DS = "demo"


def _report(items, *, status=RUN_OK, skip_reason=None, at="2026-09-28T00:00:00Z"):
    return DriftReport(
        datasource=DS, status=status, items=list(items), generated_at=at,
        skip_reason=skip_reason,
    )


def _item(subject="orders.id", *, level=L1, kind="column_removed",
          severity=SEVERITY_WARNING, detail=None):
    return DriftItem(level=level, kind=kind, subject=subject, severity=severity,
                     detail=detail or {})


@pytest.fixture
def store(tmp_path):
    return DriftStore(tmp_path)


# ── 迁移 ────────────────────────────────────────────────


async def test_schema_is_created_and_reopen_is_idempotent(store, tmp_path):
    await store.list_runs(DS)  # 触发 _ensure_schema
    assert store.db_path.exists()
    fresh = DriftStore(tmp_path)  # 第二次打开走已存在的库
    assert await fresh.list_runs(DS) == []


async def test_store_is_usable_without_explicit_migration_call(store):
    """构造即可用 —— 迁移由第一次访问惰性触发,调用方不必记得去 apply。"""
    assert await store.list_items(DS) == []


# ── I5 幂等 ─────────────────────────────────────────────


async def test_same_subject_twice_yields_one_row_with_seen_count_2(store):
    n1 = await store.record(_report([_item()], at="2026-09-28T00:00:00Z"))
    n2 = await store.record(_report([_item()], at="2026-09-28T01:00:00Z"))
    rows = await store.list_items(DS)
    assert (n1, n2) == (1, 0), "第二次检出不是新条目"
    assert len(rows) == 1
    assert rows[0].seen_count == 2
    assert rows[0].first_seen_at == "2026-09-28T00:00:00Z"
    assert rows[0].last_seen_at == "2026-09-28T01:00:00Z"


async def test_new_count_counts_only_first_sightings(store):
    await store.record(_report([_item("a")], at="2026-09-28T00:00:00Z"))
    n = await store.record(_report([_item("a"), _item("b")],
                                   at="2026-09-28T01:00:00Z"))
    assert n == 1, "只有 b 是首见"


async def test_same_table_different_levels_are_distinct_rows(store):
    """唯一键含 level:同一张表在 L1 与 L2 是两条独立证据。"""
    await store.record(_report([_item("orders", level=L1)]))
    await store.record(_report([_item("orders", level=L2, kind="dataset_table_missing")]))
    assert len(await store.list_items(DS)) == 2


# ── 重开语义 ────────────────────────────────────────────


async def test_resolved_item_reappearing_reopens_and_keeps_the_trail(store):
    """重现已解决项 = 回归。这是本层最重要的行为。"""
    await store.record(_report([_item()], at="2026-09-28T00:00:00Z"))
    row = (await store.list_items(DS))[0]
    assert await store.set_status(row.id, STATUS_RESOLVED, by="alice",
                                  reason="已补列", at="2026-09-28T02:00:00Z",
                                  allowed_from=(STATUS_OPEN,))

    await store.record(_report([_item()], at="2026-09-28T03:00:00Z"))
    after = (await store.list_items(DS))[0]
    assert after.status == STATUS_OPEN, "重现即重开,不能压在 resolved 里"
    assert after.resolved_at is None and after.resolved_by is None
    assert after.resolve_reason is None
    # 上次的处理理由并入 detail —— 「谁说了解决了、结果没解决」是最有用的线索
    assert after.detail["reopened"] == {
        "previous_resolved_at": "2026-09-28T02:00:00Z",
        "previous_resolved_by": "alice",
        "previous_reason": "已补列",
    }


async def test_waived_item_reappearing_stays_waived(store):
    """豁免是对**立场**的断言,不是对世界的 —— 世界没变,立场就不变。"""
    await store.record(_report([_item()]))
    row = (await store.list_items(DS))[0]
    await store.set_status(row.id, STATUS_WAIVED, by="alice", reason="历史表,不管",
                           at="2026-09-28T02:00:00Z", allowed_from=(STATUS_OPEN,))

    await store.record(_report([_item()], at="2026-09-28T03:00:00Z"))
    after = (await store.list_items(DS))[0]
    assert after.status == STATUS_WAIVED
    assert after.resolve_reason == "历史表,不管"
    assert after.seen_count == 2


async def test_waived_item_is_not_in_open_subjects(store):
    await store.record(_report([_item("a"), _item("b")]))
    rows = {r.subject: r for r in await store.list_items(DS)}
    await store.set_status(rows["a"].id, STATUS_WAIVED, by="x", reason="r",
                           at="t", allowed_from=(STATUS_OPEN,))
    assert await store.open_subjects(DS) == {"b"}


# ── I3:skipped 不碰条目 ─────────────────────────────────


async def test_skipped_run_records_a_row_but_touches_no_items(store):
    """「未检测」必须在历史里看得见,但**不能**刷新条目的最后可见时间。

    把 skipped 也刷一遍 ``last_seen_at``,等于宣称「这次仍然看到它了」——
    与「catalog 连不上却 exit 0」是同一个错误的两种写法。
    """
    await store.record(_report([_item()], at="2026-09-28T00:00:00Z"))
    before = (await store.list_items(DS))[0]

    n = await store.record(_report([], status=RUN_SKIPPED,
                                   skip_reason="catalog_unreachable",
                                   at="2026-09-28T05:00:00Z"))
    after = (await store.list_items(DS))[0]

    assert n == 0
    assert (after.last_seen_at, after.seen_count) == (
        before.last_seen_at, before.seen_count), "未检测不得被读成已确认仍在"

    runs = await store.list_runs(DS)
    assert len(runs) == 2
    assert runs[0]["status"] == RUN_SKIPPED
    assert runs[0]["skip_reason"] == "catalog_unreachable"
    assert runs[0]["detected"] == 0 and runs[0]["new_count"] == 0


async def test_clean_run_and_skipped_run_are_distinguishable_in_history(store):
    await store.record(_report([], at="2026-09-28T00:00:00Z"))
    await store.record(_report([], status=RUN_SKIPPED, skip_reason="kb_missing",
                               at="2026-09-28T01:00:00Z"))
    runs = {r["started_at"]: r for r in await store.list_runs(DS)}
    assert runs["2026-09-28T00:00:00Z"]["status"] == RUN_OK
    assert runs["2026-09-28T01:00:00Z"]["status"] == RUN_SKIPPED


async def test_run_row_records_detected_and_new_counts(store):
    await store.record(_report([_item("a")], at="2026-09-28T00:00:00Z"))
    await store.record(_report([_item("a"), _item("b")],
                               at="2026-09-28T01:00:00Z"))
    run = (await store.list_runs(DS))[0]
    assert (run["detected"], run["new_count"]) == (2, 1)


# ── 状态机 ──────────────────────────────────────────────


async def test_set_status_refuses_transition_outside_allowed_from(store):
    await store.record(_report([_item()]))
    row = (await store.list_items(DS))[0]
    await store.set_status(row.id, STATUS_RESOLVED, by="a", reason="r", at="t",
                           allowed_from=(STATUS_OPEN,))

    # 已 resolved,再次 resolve → False(调用方据此回 409),且状态不被改写
    ok = await store.set_status(row.id, STATUS_RESOLVED, by="b", reason="又解决一次",
                                at="t2", allowed_from=(STATUS_OPEN,))
    assert ok is False
    assert (await store.list_items(DS))[0].resolved_by == "a"


async def test_set_status_on_missing_id_returns_false(store):
    ok = await store.set_status(99999, STATUS_RESOLVED, by="a", reason="r",
                                at="t", allowed_from=(STATUS_OPEN,))
    assert ok is False


async def test_set_status_rejects_unknown_status(store):
    await store.record(_report([_item()]))
    row = (await store.list_items(DS))[0]
    with pytest.raises(ValueError):
        await store.set_status(row.id, "skipped", by="a", reason="r", at="t",
                               allowed_from=(STATUS_OPEN,))


# ── 读取 ────────────────────────────────────────────────


async def test_list_items_orders_by_real_severity_not_text(store):
    """severity 是文本列,字典序与真实严重度不一致(critical < info < warning)。"""
    await store.record(_report([
        _item("a", severity=SEVERITY_INFO),
        _item("b", severity=SEVERITY_CRITICAL),
        _item("c", severity=SEVERITY_WARNING),
    ]))
    got = [r.subject for r in await store.list_items(DS)]
    assert got == ["b", "c", "a"]


async def test_list_items_filters_by_status_and_level(store):
    await store.record(_report([_item("a", level=L1), _item("b", level=L2)]))
    rows = {r.subject: r for r in await store.list_items(DS)}
    await store.set_status(rows["a"].id, STATUS_WAIVED, by="x", reason="r",
                           at="t", allowed_from=(STATUS_OPEN,))

    assert await store.list_items(DS, level=L2) != []
    assert [r.subject for r in await store.list_items(DS, level=L2)] == ["b"]
    assert [r.subject for r in await store.list_items(DS, status=STATUS_WAIVED)] == ["a"]
    assert [r.subject for r in await store.list_items(DS, include_waived=False)] == ["b"]


async def test_list_items_is_scoped_to_one_datasource(store):
    await store.record(_report([_item("a")]))
    other = DriftReport(datasource="other", status=RUN_OK,
                        items=[_item("z")], generated_at="2026-09-28T00:00:00Z")
    await store.record(other)
    assert [r.subject for r in await store.list_items(DS)] == ["a"]


async def test_get_item_round_trips_detail_and_affected(store):
    await store.record(_report([_item(detail={"relationship": "r1",
                                             "problems": "t gone"})]))
    row = (await store.list_items(DS))[0]
    got = await store.get_item(row.id)
    assert got is not None
    assert got.detail == {"relationship": "r1", "problems": "t gone"}
    # affected 存的是 ImpactSet.to_dict() 的六个键,不是 {} —— 形状稳定,
    # 读回来的人不必先判空再取键。
    assert got.affected == {"metrics": [], "examples": [], "rules": [],
                            "lessons": [], "topics": [], "basis": {}}


async def test_get_item_returns_none_for_missing(store):
    assert await store.get_item(4242) is None


# ── 外部声明 ────────────────────────────────────────────


async def test_declare_external_marks_source_and_upserts(store):
    for n in (1, 2):
        await store.declare_external(
            datasource=DS, level="L4", kind="semantics_changed",
            subject="orders", detail={"note": f"第 {n} 次"},
            severity=SEVERITY_WARNING, declared_at=f"2026-09-28T0{n}:00:00Z",
            author="alice",
        )
    rows = await store.list_items(DS)
    assert len(rows) == 1
    assert rows[0].source == SOURCE_EXTERNAL
    assert rows[0].seen_count == 2
    assert rows[0].detail["declared_by"] == "alice"
    assert rows[0].detail["note"] == "第 2 次"


async def test_external_declaration_coexists_with_detector_item(store):
    """机器结论与人的告知可信度来源不同 —— 同主体也不应互相覆盖。"""
    await store.record(_report([_item("orders", level=L1, kind="table_removed")]))
    await store.declare_external(datasource=DS, level="L4",
                                 kind="semantics_changed", subject="orders",
                                 detail={}, severity=SEVERITY_INFO,
                                 declared_at="2026-09-28T00:00:00Z")
    assert len(await store.list_items(DS)) == 2
