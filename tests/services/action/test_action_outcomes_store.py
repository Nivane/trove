"""``outcomes`` 表(闭环验收 B7):迁移 v2 收养 + 四个方法的语义。

三条被钉住的纪律:

  - **0/1/NULL 三态**在写入与读回两侧都保真(``None`` 不是 ``False``:
    "判不了"与"判了,没变化"是两种结论);
  - ``list_unmeasured_proposals`` 是**一次测量一个结局**:测过(哪怕
    是 error 行)就不再是候选;
  - ``list_effect_entries`` 做 JOIN 投影 —— rule_id 从提案取(提案
    不变),rule_rev 从测量行取(测量自己记的版本)。
"""

from __future__ import annotations

import pytest

from trove.services.action.models import Outcome
from trove.services.action.store import STORE_NAME, ActionStore, _migrations
from trove.storage.migrations import SQLITE, apply_migrations

from tests.services.action.test_action_store import _proposal  # noqa: F401


@pytest.fixture()
async def store(tmp_path):
    s = ActionStore(tmp_path)
    try:
        yield s
    finally:
        await s.dispose()


def _outcome(**kw) -> Outcome:
    base = dict(
        proposal_id="p-1", measured_at="2026-10-05T03:00:00",
        window_start="2026-10-01", window_end="2026-10-31",
        metric="loan_balance", rule_rev="rev-1",
        delta=49.5, pct=0.49, outside_band=True, z=4.2, method="its+did",
        confidence=0.2, observed={"sql": "SELECT ...", "blocks": 9},
    )
    base.update(kw)
    return Outcome(**base)


async def _dispatch(store, pid="p-1", **kw):
    """提案直接摆成 dispatched(测的是测量面,不是状态机)。"""
    await store.create_proposal(_proposal(id=pid, idempotency_key="k-" + pid,
                                          **kw))
    await store.update_proposal(
        pid, status="dispatched",
        dispatched_at=kw.get("dispatched_at", "2026-10-01T09:00:00"))


class TestRoundTrip:
    async def test_all_fields(self, store):
        await store.create_proposal(_proposal())
        row_id = await store.record_outcome(_outcome())
        assert row_id > 0
        (o,) = await store.list_outcomes("p-1")
        assert o.id == row_id
        assert o.measured_at == "2026-10-05T03:00:00"
        assert (o.window_start, o.window_end) == ("2026-10-01", "2026-10-31")
        assert o.metric == "loan_balance" and o.rule_rev == "rev-1"
        assert o.delta == pytest.approx(49.5)
        assert o.pct == pytest.approx(0.49)
        assert o.z == pytest.approx(4.2)
        assert o.method == "its+did"
        assert o.confidence == pytest.approx(0.2)
        assert o.observed["blocks"] == 9
        assert o.error == ""

    @pytest.mark.parametrize("band,expected", [
        (True, True), (False, False), (None, None)])
    async def test_outside_band_three_state(self, store, band, expected):
        await store.create_proposal(_proposal())
        await store.record_outcome(_outcome(outside_band=band))
        (o,) = await store.list_outcomes("p-1")
        assert o.outside_band is expected

    async def test_error_row_keeps_nulls_not_zeros(self, store):
        await store.create_proposal(_proposal())
        await store.record_outcome(_outcome(
            delta=None, pct=None, z=None, confidence=None,
            outside_band=None, error="group_unresolved"))
        (o,) = await store.list_outcomes("p-1")
        assert o.error == "group_unresolved"
        assert o.delta is None and o.pct is None and o.z is None
        assert o.confidence is None and o.outside_band is None

    async def test_list_is_scoped_and_ordered(self, store):
        await store.create_proposal(_proposal())
        await store.create_proposal(_proposal(id="p-2", idempotency_key="k2"))
        await store.record_outcome(_outcome(measured_at="2026-10-05T00:00:00"))
        await store.record_outcome(_outcome(proposal_id="p-2", rule_rev="rev-2"))
        await store.record_outcome(_outcome(measured_at="2026-11-05T00:00:00"))

        rows = await store.list_outcomes("p-1")
        assert [r.measured_at for r in rows] == [
            "2026-10-05T00:00:00", "2026-11-05T00:00:00"]
        assert [r.rule_rev for r in await store.list_outcomes("p-2")] == ["rev-2"]
        assert await store.list_outcomes("nope") == []


class TestUnmeasuredCandidates:
    async def test_only_dispatched_and_delivered(self, store):
        await store.create_proposal(_proposal(id="p-pending",
                                              idempotency_key="k1"))
        await _dispatch(store, "p-dispatched")
        await _dispatch(store, "p-delivered")
        await store.update_proposal("p-delivered", status="delivered")
        await store.update_proposal("p-pending", status="approved")

        ids = [p.id for p in await store.list_unmeasured_proposals(
            ("dispatched", "delivered"))]
        assert ids == ["p-delivered", "p-dispatched"]

    async def test_measured_is_no_longer_a_candidate(self, store):
        await _dispatch(store, "p-1")
        await store.record_outcome(_outcome())
        assert await store.list_unmeasured_proposals(
            ("dispatched", "delivered")) == []

    async def test_error_outcome_also_counts_as_measured(self, store):
        """测过(哪怕失败)= 已有结局 —— 不重试(一次测量,一个结局)。"""
        await _dispatch(store, "p-1")
        await store.record_outcome(_outcome(
            outside_band=None, delta=None, error="verifier_failed: x"))
        assert await store.list_unmeasured_proposals(
            ("dispatched", "delivered")) == []

    async def test_oldest_dispatch_first_and_limit(self, store):
        await _dispatch(store, "p-late", dispatched_at="2026-10-03T09:00:00")
        await _dispatch(store, "p-early", dispatched_at="2026-10-01T09:00:00")
        assert [p.id for p in await store.list_unmeasured_proposals(
            ("dispatched",))] == ["p-early", "p-late"]
        assert [p.id for p in await store.list_unmeasured_proposals(
            ("dispatched",), limit=1)] == ["p-early"]

    async def test_other_statuses_never_leak_in(self, store):
        await store.create_proposal(_proposal(id="p-failed",
                                              idempotency_key="k"))
        await store.update_proposal("p-failed", status="failed")
        assert await store.list_unmeasured_proposals(
            ("dispatched", "delivered")) == []


class TestEffectEntries:
    async def test_projection_and_join(self, store):
        await _dispatch(store, "p-1")
        await store.create_proposal(_proposal(
            id="p-2", idempotency_key="k2", datasource="other",
            rule_id="other-rule"))
        await store.update_proposal(
            "p-2", status="dispatched",
            dispatched_at="2026-10-01T09:00:00")

        await store.record_outcome(_outcome(rule_rev="rev-a"))
        await store.record_outcome(_outcome(
            proposal_id="p-2", rule_rev="rev-b", outside_band=False,
            error="", pct=-0.1, measured_at="2026-10-06T00:00:00"))

        entries = await store.list_effect_entries("financial")
        assert len(entries) == 1                      # datasource 过滤
        e = entries[0]
        assert set(e) == {"rule_id", "rule_rev", "outside_band", "error",
                          "measured_at", "window_end", "proposal_id", "pct"}
        assert e["rule_id"] == "revenue-drop"          # 来自提案行
        assert e["rule_rev"] == "rev-a"                # 来自测量行
        assert e["outside_band"] is True
        assert e["window_end"] == "2026-10-31"

        other = await store.list_effect_entries("other")
        assert [(x["rule_id"], x["rule_rev"]) for x in other] == [
            ("other-rule", "rev-b")]
        assert other[0]["outside_band"] is False       # 0/1 → False 保真

    async def test_truly_unmeasured_proposals_do_not_appear(self, store):
        await _dispatch(store, "p-1")
        assert await store.list_effect_entries("financial") == []

    async def test_newest_first_with_limit(self, store):
        await _dispatch(store, "p-1")
        for day in ("05", "06", "07"):
            await store.record_outcome(_outcome(
                rule_rev=f"rev-{day}", measured_at=f"2026-10-{day}T00:00:00"))
        entries = await store.list_effect_entries("financial", limit=2)
        assert [e["rule_rev"] for e in entries] == ["rev-07", "rev-06"]


class TestMigrationV2:
    async def test_legacy_v1_db_is_adopted_and_upgraded(self, tmp_path):
        """老代码建的 v1 库(三表、有数据、没有 outcomes 表)→ 打开即收养。"""
        db_path = tmp_path / ".trove" / "actions" / "actions.sqlite"
        db_path.parent.mkdir(parents=True)
        from trove.storage.backends import SqliteBackend

        backend = SqliteBackend(str(db_path))
        try:
            await apply_migrations(backend, STORE_NAME, _migrations()[:1],
                                   dialect=SQLITE)
            await backend.execute(
                "INSERT INTO proposals (id, datasource, rule_id, status,"
                " created_at, dispatched_at) VALUES (?,?,?,?,?,?)",
                ("p-legacy", "financial", "revenue-drop", "dispatched",
                 "2026-10-01T08:00:00", "2026-10-01T09:00:00"))
            await backend.commit()
        finally:
            await backend.dispose()

        s = ActionStore(tmp_path)
        try:
            fresh = await s.get_proposal("p-legacy")
            assert fresh is not None and fresh.status == "dispatched"
            await s.record_outcome(_outcome(proposal_id="p-legacy"))
            assert len(await s.list_outcomes("p-legacy")) == 1
        finally:
            await s.dispose()

    async def test_fresh_and_reopened_both_carry_the_table(self, tmp_path):
        s1 = ActionStore(tmp_path)
        try:
            await s1.create_proposal(_proposal())
            await s1.record_outcome(_outcome())
        finally:
            await s1.dispose()
        s2 = ActionStore(tmp_path)
        try:
            assert len(await s2.list_outcomes("p-1")) == 1
        finally:
            await s2.dispose()

    async def test_proposal_sql_literals_stay_in_sync_with_the_columns(self):
        """三处同步:SELECT 常量 ↔ 行映射 ↔ 表定义(加列时最安静的那种错)。"""
        from trove.services.action.store import (_OUTCOME_COLUMNS,
                                                 _OUTCOMES_SQL)

        for col in ("id", "proposal_id", "measured_at", "window_start",
                    "window_end", "metric", "rule_rev", "delta", "pct",
                    "outside_band", "z", "method", "confidence",
                    "observed_json", "error"):
            assert col in _OUTCOME_COLUMNS
            assert col in _OUTCOMES_SQL
        assert len(_OUTCOME_COLUMNS.split(",")) == 15
