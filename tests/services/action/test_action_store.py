"""ActionStore:三表往返、幂等键唯一索引、过期清收、迁移幂等。

fixture 一律 ``await store.dispose()`` —— aiosqlite 的工作线程不是守护
线程,不关连接 pytest 会「全过但进程不退出」。
"""

from __future__ import annotations

import pytest

from trove.services.action.models import ActionProposal, Approval, Delivery
from trove.services.action.propose import ProposalError
from trove.services.action.store import ActionStore


@pytest.fixture()
async def store(tmp_path):
    s = ActionStore(tmp_path)
    try:
        yield s
    finally:
        await s.dispose()


def _proposal(**kw) -> ActionProposal:
    base = dict(
        id="p-1", datasource="financial", rule_id="revenue-drop",
        rule_digest="d1", origin_kind="verdict", action_type="notify",
        template="notify-ops", template_digest="t1",
        target={"channel": "ops-alerts", "resource": "#ops"},
        payload={"rule": "revenue-drop", "n": 1},
        rationale="check calendar", evidence_refs={"run_id": 7},
        severity="warning", priority=2, risk="medium", status="pending",
        idempotency_key="key-1", created_by="system",
        created_at="2026-10-03T09:00:00", expires_at="2026-10-06T09:00:00",
    )
    base.update(kw)
    return ActionProposal(**base)


async def test_round_trip_all_fields(store):
    await store.create_proposal(_proposal())
    p = await store.get_proposal("p-1")
    assert p is not None
    assert p.payload == {"rule": "revenue-drop", "n": 1}
    assert p.target["channel"] == "ops-alerts"
    assert p.evidence_refs == {"run_id": 7}
    assert p.idempotency_key == "key-1"
    assert p.expires_at == "2026-10-06T09:00:00"
    assert p.attempts == 0 and p.error == ""


async def test_missing_proposal_is_none(store):
    assert await store.get_proposal("nope") is None


async def test_idempotency_lookup(store):
    await store.create_proposal(_proposal())
    hit = await store.find_by_idempotency_key("key-1")
    assert hit is not None and hit.id == "p-1"
    assert await store.find_by_idempotency_key("other") is None
    # 空键直接 None(部分唯一索引同样忽略空键)
    await store.create_proposal(_proposal(id="p-2", idempotency_key=""))
    await store.create_proposal(_proposal(id="p-3", idempotency_key=""))
    assert await store.find_by_idempotency_key("") is None


async def test_duplicate_idempotency_key_is_refused_by_the_index(store):
    """去重由数据库保证,不是 service 里的 read-then-write —— 两个 tick
    同时插入时只有一条能过。"""
    await store.create_proposal(_proposal())
    with pytest.raises(Exception):
        await store.create_proposal(_proposal(id="p-1-bis"))


async def test_list_proposals_filters_and_orders(store):
    await store.create_proposal(_proposal(
        id="p-1", created_at="2026-10-01T09:00:00", status="pending"))
    await store.create_proposal(_proposal(
        id="p-2", idempotency_key="key-2", created_at="2026-10-03T09:00:00",
        status="approved"))
    await store.create_proposal(_proposal(
        id="p-3", idempotency_key="key-3", created_at="2026-10-02T09:00:00",
        status="pending", datasource="other"))

    newest = await store.list_proposals()
    assert [p.id for p in newest] == ["p-2", "p-3", "p-1"]
    assert [p.id for p in await store.list_proposals(status="pending")] == \
        ["p-3", "p-1"]
    assert [p.id for p in await store.list_proposals(datasource="financial")] \
        == ["p-2", "p-1"]
    assert [p.id for p in await store.list_proposals(limit=1)] == ["p-2"]


async def test_count_by_status(store):
    await store.create_proposal(_proposal(id="p-1"))
    await store.create_proposal(_proposal(
        id="p-2", idempotency_key="k2", status="approved"))
    await store.create_proposal(_proposal(
        id="p-3", idempotency_key="k3", status="approved"))
    assert await store.count_by_status() == {"pending": 1, "approved": 2}


async def test_update_proposal_touches_lifecycle_columns_only(store):
    await store.create_proposal(_proposal())
    await store.update_proposal(
        "p-1", status="approved", decided_at="2026-10-03T10:00:00",
        attempts=1, error="x")
    p = await store.get_proposal("p-1")
    assert p.status == "approved" and p.attempts == 1 and p.error == "x"
    with pytest.raises(ValueError) as e:
        await store.update_proposal("p-1", payload={"tampered": True})
    assert "immutable" in str(e.value)
    assert (await store.get_proposal("p-1")).payload == _proposal().payload


async def test_update_proposal_expect_status_is_a_conditional_write(store):
    """``expect_status`` 是条件更新(读-改-写竞态的关门闸):状态对得上才写,
    对不上抛 ``ProposalError`` 且**生命周期的列一个字节没动** —— 两个并发
    决定只有一个能落。"""
    await store.create_proposal(_proposal())

    await store.update_proposal(
        "p-1", expect_status="pending", status="approved",
        decided_at="2026-10-03T10:00:00")
    assert (await store.get_proposal("p-1")).status == "approved"

    # 读到的 pending 已被并发方改走 → 拒绝,且不覆盖赢家写下的任何一列
    with pytest.raises(ProposalError) as e:
        await store.update_proposal(
            "p-1", expect_status="pending", status="rejected",
            decided_at="2026-10-03T11:00:00")
    assert "no longer 'pending'" in str(e.value)
    p = await store.get_proposal("p-1")
    assert p.status == "approved" and p.decided_at == "2026-10-03T10:00:00"

    # 行不存在与状态不符走同一条错误路径(rowcount=0 不区分 —— 消息里带上
    # 期望状态,调用方据此知道该重读什么)
    with pytest.raises(ProposalError) as e:
        await store.update_proposal(
            "p-ghost", expect_status="pending", status="approved")
    assert "'pending'" in str(e.value)


async def test_update_proposal_without_expect_status_stays_unconditional(store):
    """兼容安全带:不传 ``expect_status`` 的老调用点行为逐字节不变 ——
    裸 UPDATE 仍然照写(旧调用方依赖的就是它),条件闸只对显式声明的调用
    生效。"""
    await store.create_proposal(_proposal(status="approved"))
    await store.update_proposal("p-1", status="cancelled")
    assert (await store.get_proposal("p-1")).status == "cancelled"


async def test_expire_due_only_touches_overdue_pending(store):
    await store.create_proposal(_proposal(
        id="p-due", expires_at="2026-10-03T09:00:00"))
    await store.create_proposal(_proposal(
        id="p-future", idempotency_key="k2", expires_at="2026-10-09T09:00:00"))
    await store.create_proposal(_proposal(
        id="p-approved", idempotency_key="k3", status="approved",
        expires_at="2026-10-03T08:00:00"))
    await store.create_proposal(_proposal(
        id="p-no-expiry", idempotency_key="k4", expires_at=""))

    # 严格小于:恰好到点的还留给读它的人一瞬(与 _is_expired 同一口径)
    assert await store.expire_due("2026-10-03T09:00:00") == 0
    count = await store.expire_due("2026-10-03T09:00:01")
    assert count == 1
    assert (await store.get_proposal("p-due")).status == "expired"
    assert (await store.get_proposal("p-future")).status == "pending"
    assert (await store.get_proposal("p-approved")).status == "approved"
    assert (await store.get_proposal("p-no-expiry")).status == "pending"
    assert await store.expire_due("2026-10-03T09:00:01") == 0


async def test_approvals_and_deliveries_are_append_only(store):
    await store.create_proposal(_proposal())
    await store.add_approval(Approval(
        proposal_id="p-1", user_id="admin", action="approve",
        comment="go", created_at="2026-10-03T10:00:00"))
    await store.add_approval(Approval(
        proposal_id="p-1", user_id="admin", action="dispatch",
        created_at="2026-10-03T10:01:00"))
    approvals = await store.list_approvals("p-1")
    assert [a.action for a in approvals] == ["approve", "dispatch"]
    assert approvals[0].comment == "go"

    await store.add_delivery(Delivery(
        proposal_id="p-1", channel="ops-alerts", status="sent",
        http_status=200, response_excerpt="ok",
        attempted_at="2026-10-03T10:01:00"))
    await store.add_delivery(Delivery(
        proposal_id="p-1", channel="ack", status="ack",
        attempted_at="2026-10-03T10:05:00"))
    deliveries = await store.list_deliveries("p-1")
    assert [d.status for d in deliveries] == ["sent", "ack"]
    assert deliveries[0].http_status == 200
    assert await store.list_approvals("other") == []
    assert await store.list_deliveries("other") == []


async def test_schema_setup_is_idempotent(tmp_path):
    """同一路径开两次(迁移重放)= 空操作,数据还在。"""
    s1 = ActionStore(tmp_path)
    try:
        await s1.create_proposal(_proposal())
    finally:
        await s1.dispose()
    s2 = ActionStore(tmp_path)
    try:
        assert (await s2.get_proposal("p-1")) is not None
        await s2.create_proposal(_proposal(id="p-2", idempotency_key="k2"))
        assert len(await s2.list_proposals()) == 2
    finally:
        await s2.dispose()
