"""DriftService —— 检测编排、级别过滤、处置状态机、外部声明。

这个服务存在的意义是**一处接线**:在此之前 L1 检测只有生命周期巡检在调、
L2 检测只有 ``check_drift.py`` 在调,两者在服务层从不相遇。所以这里测的重点
是「两条路是否真的合到了一起」,以及级别请求的边界。

``schema_provider`` 是注入的,所以整份测试不需要数据库 —— 这正是把它做成
回调的原因。
"""

from __future__ import annotations

import pytest

from trove.services.kb.service import KbService
from trove.services.drift.models import (
    L1,
    L2,
    L3,
    RUN_OK,
    RUN_SKIPPED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    STATUS_WAIVED,
)
from trove.services.drift.service import (
    DriftError,
    DriftNotFound,
    DriftService,
    ExternalDrift,
    IllegalTransition,
    UnknownLevel,
)

DS = "demo"
SCHEMA = {"orders": {"id", "amount"}, "users": {"id"}}


def _write_kb(root, tables: dict[str, set[str]], datasource: str = DS) -> KbService:
    """写一份真的 ``schema_notes.yml`` 并返回真的 ``KbService``。

    不用替身:``detect_drift`` 走的是 ``_kb_column_set`` —— 直接解析 YAML
    文件,而不是调 KB 的方法。所以替身必须连文件系统一起假,那测的就不是
    同一条路径了。
    """
    kb = KbService(root / "proj")
    path = kb.kb_dir / datasource / "schema_notes.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["tables:"]
    for table, cols in tables.items():
        lines.append(f"  - name: {table}")
        lines.append("    columns:")
        for col in sorted(cols):
            lines.append(f"      - name: {col}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return kb


class _FakeSemantic:
    def __init__(self, report):
        self._report = report

    def drift(self):
        return self._report


def _ok_semantic(**overrides):
    base = {"stale": False, "gone_tables": [], "missing_fields": {},
            "missing_keys": {}, "relationship_breaks": [],
            "status": RUN_OK, "skip_reason": None}
    base.update(overrides)
    return base


async def _schema(datasource):
    return SCHEMA


#: 活库与 KB 一致的基线 —— 用它做默认,让每条测试只表达**自己那一处**差异。
KB_IN_SYNC = {"orders": {"id", "amount"}, "users": {"id"}}

_UNSET = object()
#: 传给 ``semantic=`` 表示「这个数据源**没有**语义层」(不适用),
#: 与「压根没接语义层」是两件事 —— 前者 L2 空过,后者整体 skipped。
NO_LAYER = object()


def _service(tmp_path, *, kb_tables=None, semantic=_UNSET, schema=_schema,
             kb=True, **kw):
    """默认两边都正常:活库与 KB 一致、语义层干净。

    ``semantic`` 三态:不传 = 有一个干净的语义层;``NO_LAYER`` = 该库没有
    语义层;``None`` = 服务压根没接语义层。
    """
    if semantic is _UNSET:
        async def factory(ds, catalog):
            return _FakeSemantic(_ok_semantic())
    elif semantic is NO_LAYER:
        async def factory(ds, catalog):
            return None
    elif semantic is None:
        factory = None
    else:
        async def factory(ds, catalog):
            return _FakeSemantic(semantic)

    return DriftService(
        tmp_path,
        kb=(_write_kb(tmp_path, kb_tables if kb_tables is not None else KB_IN_SYNC)
            if kb else None),
        schema_provider=schema,
        semantic_factory=factory,
        **kw,
    )


# ── 合流 ────────────────────────────────────────────────


async def test_both_levels_run_and_land_in_one_report(tmp_path):
    """L1 与 L2 的发现出现在**同一份**报告里 —— 这正是本服务存在的理由。"""
    svc = _service(
        tmp_path,
        kb_tables={"orders": {"id"}},               # 活库多出 orders.amount → L1
        semantic=_ok_semantic(gone_tables=["users"]),  # semantics.yml 声明 users → L2
    )
    report = await svc.detect(DS)

    assert report.status == RUN_OK
    assert report.levels_verified == frozenset({L1, L2})
    assert {i.level for i in report.items} == {L1, L2}
    assert "orders.amount" in {i.subject for i in report.items}
    assert "users" in {i.subject for i in report.items}


async def test_report_is_persisted_and_second_run_reports_zero_new(tmp_path):
    svc = _service(tmp_path, kb_tables={"orders": {"id"}})
    first = await svc.detect(DS)
    assert first.new_count == len(first.items) > 0

    second = await svc.detect(DS)
    assert second.new_count == 0, "同一个主体第二次检出不是新的"
    rows = await svc.list(DS)
    assert len(rows) == len(first.items)
    assert all(r.seen_count == 2 for r in rows)
    await svc.dispose()


# ── I3:未检测必须是 skipped ─────────────────────────────


async def test_catalog_unreachable_yields_skipped_not_clean(tmp_path):
    async def boom(datasource):
        raise RuntimeError("connection refused")

    svc = _service(tmp_path, schema=boom, semantic=_ok_semantic())
    report = await svc.detect(DS)

    assert report.status == RUN_SKIPPED
    assert report.items == []
    assert "catalog_unreachable" in (report.skip_reason or "")
    assert report.levels_verified == frozenset(), "未完成的检测什么都没验证"
    await svc.dispose()


async def test_skipped_detection_does_not_refresh_existing_items(tmp_path):
    svc = _service(tmp_path, kb_tables={"orders": {"id"}})
    await svc.detect(DS)
    before = (await svc.list(DS))[0]

    async def boom(datasource):
        raise RuntimeError("down")

    svc._schema_provider = boom
    await svc.detect(DS)
    after = (await svc.list(DS))[0]

    assert (after.last_seen_at, after.seen_count) == (
        before.last_seen_at, before.seen_count)
    runs = await svc.runs(DS)
    assert runs[0]["status"] == RUN_SKIPPED and runs[0]["detected"] == 0
    await svc.dispose()


async def test_no_semantic_layer_is_not_applicable_and_l2_is_unverified(tmp_path):
    """datasource 没有语义层 → L2 不适用,**但也不能记成「L2 已验证」。**"""
    svc = _service(tmp_path, semantic=NO_LAYER)
    report = await svc.detect(DS)
    assert report.status == RUN_OK
    assert report.levels_verified == frozenset({L1})
    assert not report.verified(L2)
    await svc.dispose()


async def test_unwired_semantic_factory_degrades_to_skipped(tmp_path):
    """连语义层都没接 = 无法声称 L2 干净。保守降级,不是静默放行。"""
    svc = _service(tmp_path, semantic=None)
    report = await svc.detect(DS)
    assert report.status == RUN_SKIPPED
    assert report.skip_reason == "no_semantic_model"
    assert report.levels_verified == frozenset()
    await svc.dispose()


async def test_missing_kb_file_degrades_to_skipped(tmp_path):
    """没建过 KB 也不等于「无漂移」—— 没有基准可比,结论无从谈起。"""
    svc = _service(tmp_path, kb=False)
    report = await svc.detect(DS)
    assert report.status == RUN_SKIPPED
    assert "kb_missing" in (report.skip_reason or "")
    await svc.dispose()


# ── levels 边界 ─────────────────────────────────────────


async def test_requesting_only_l1_does_not_claim_l2_is_clean(tmp_path):
    svc = _service(tmp_path, kb_tables={"orders": {"id"}},
                   semantic=_ok_semantic(gone_tables=["users"]))
    report = await svc.detect(DS, levels={L1})
    assert report.levels_verified == frozenset({L1})
    assert {i.level for i in report.items} == {L1}
    await svc.dispose()


async def test_requesting_unimplemented_level_raises_instead_of_clean(tmp_path):
    """请求 L3 拿到一份「干净」报告是最糟的结果 —— 明确报错。"""
    svc = _service(tmp_path)
    with pytest.raises(UnknownLevel) as exc:
        await svc.detect(DS, levels={L3})
    assert "L3" in str(exc.value)
    await svc.dispose()


async def test_requesting_bogus_level_raises(tmp_path):
    svc = _service(tmp_path)
    with pytest.raises(UnknownLevel):
        await svc.detect(DS, levels={"L9"})
    await svc.dispose()


# ── 处置 ────────────────────────────────────────────────


async def test_resolve_then_waive_conflicts(tmp_path):
    svc = _service(tmp_path, kb_tables={"orders": {"id"}})
    await svc.detect(DS)
    row = (await svc.list(DS))[0]

    await svc.resolve(row.id, by="alice", reason="已补列")
    assert (await svc.get(row.id)).status == STATUS_RESOLVED

    with pytest.raises(IllegalTransition):
        await svc.waive(row.id, by="bob", reason="算了")
    await svc.dispose()


async def test_resolve_requires_a_reason(tmp_path):
    """无理由的 resolve 等于删记录。"""
    svc = _service(tmp_path, kb_tables={"orders": {"id"}})
    await svc.detect(DS)
    row = (await svc.list(DS))[0]
    with pytest.raises(IllegalTransition):
        await svc.resolve(row.id, by="alice", reason="   ")
    assert (await svc.get(row.id)).status == STATUS_OPEN
    await svc.dispose()


async def test_resolve_missing_id_is_not_found(tmp_path):
    svc = _service(tmp_path)
    with pytest.raises(DriftNotFound):
        await svc.resolve(9999, by="alice", reason="r")
    await svc.dispose()


async def test_waived_items_are_not_treated_as_regression_on_next_detect(tmp_path):
    """豁免是立场不是事实:再次检出是预期之内,不该重开也不该再阻断。"""
    svc = _service(tmp_path, kb_tables={"orders": {"id"}})
    await svc.detect(DS)
    rows = await svc.list(DS)
    assert len(rows) > 1, "基线偏差应有不止一处,否则这条测试测不到批量"
    for row in rows:
        await svc.waive(row.id, by="alice", reason="历史遗留,不管")

    await svc.detect(DS)
    statuses = [(await svc.get(r.id)).status for r in rows]
    assert statuses == [STATUS_WAIVED] * len(rows)
    assert await svc.open_subjects(DS) == set(), "豁免过的不该再阻断下一次变更"
    await svc.dispose()


async def test_impact_reads_the_frozen_snapshot(tmp_path):
    svc = _service(tmp_path, kb_tables={"orders": {"id"}})
    await svc.detect(DS)
    row = (await svc.list(DS))[0]
    impact = await svc.impact(row.id)
    assert impact.is_empty(), "P2 的 ImpactResolver 未接前恒为空,但形状已定"
    await svc.dispose()


# ── 外部声明(L4)────────────────────────────────────────


async def test_declare_external_normalizes_subject(tmp_path):
    svc = _service(tmp_path)
    row = await svc.declare_external(ExternalDrift(
        datasource=DS, subject='"PUBLIC"."orders"."amount"', author="alice",
        detail={"note": "上游改了 approved 的判定"},
    ))
    assert row.subject == "orders.amount", "subject 必须与检测器同一套规范化"
    assert row.source == "external"
    await svc.dispose()


async def test_two_segment_subject_keeps_both_segments(tmp_path):
    """两段是歧义的(``schema.table`` 还是 ``table.column``),取「保留」。

    剥离的代价是把 ``orders.id`` 与 ``users.id`` 一起塌成 ``id`` —— 不同表
    的同名列合并成一条记录,``seen_count`` 从此说谎。少合并好过错合并。
    """
    svc = _service(tmp_path)
    row = await svc.declare_external(ExternalDrift(
        datasource=DS, subject="public.orders", author="alice"))
    assert row.subject == "public.orders"
    await svc.dispose()


async def test_declare_external_requires_subject_and_valid_severity(tmp_path):
    svc = _service(tmp_path)
    with pytest.raises(DriftError):
        await svc.declare_external(ExternalDrift(datasource=DS, subject="   "))
    with pytest.raises(DriftError):
        await svc.declare_external(ExternalDrift(
            datasource=DS, subject="orders", severity="catastrophic"))
    await svc.dispose()


async def test_external_declaration_survives_a_later_detect(tmp_path):
    """外部声明是 L4,检测器不产出 L4 —— 一次 L1/L2 检测不该把它冲掉。"""
    svc = _service(tmp_path)
    row = await svc.declare_external(ExternalDrift(
        datasource=DS, subject="orders", author="alice"))

    await svc.detect(DS)
    still = await svc.get(row.id)
    assert still.source == "external"


# ── 运行历史 ────────────────────────────────────────────


async def test_runs_history_includes_ok_and_skipped(tmp_path):
    svc = _service(tmp_path, kb_tables={"orders": {"id"}})
    await svc.detect(DS)

    async def boom(datasource):
        raise RuntimeError("down")

    svc._schema_provider = boom
    await svc.detect(DS)

    runs = await svc.runs(DS)
    assert [r["status"] for r in runs] == [RUN_SKIPPED, RUN_OK]
    await svc.dispose()
