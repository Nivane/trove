"""ActionService 状态机:两道门 / 过期 / 重试上限 / 禁用即双向拒绝 / 回执。

外送走真实 ``ActionDispatcher`` + 注入的假 transport(零真实网络),所以
通道解析、``_trove`` 信封、失败落库这些路径都是被真代码走过的。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from trove.services.action.dispatcher import ActionDispatcher
from trove.services.action.models import OPEN_STATUSES
from trove.services.action.propose import ProposalError
from trove.services.action.service import ActionService
from trove.services.action.template_render import RenderError
from trove.services.action.store import ActionStore
from trove.services.action.templates import ActionTemplateService
from trove.services.decision.rules import ActionRef, DecisionRule, Subject
from trove.services.decision.service import DecisionOutcome

NOW = datetime(2026, 10, 3, 9, 0, 0)
ANCHOR = "2026-10-03"
PAYLOAD_TEMPLATE = ('{"rule": "{{rule_id}}", "metric": "{{metric}}", '
                    '"current": {{current}}, "msg": "{{message}}"}')
CHANNEL_URL = "https://hook.invalid/ops"


# ── 固件 ────────────────────────────────────────────────

@pytest.fixture()
async def env(tmp_path):
    store = ActionStore(tmp_path)
    templates = ActionTemplateService(tmp_path / "actions")
    templates.create({
        "name": "notify-ops", "title": "Notify ops",
        "target": {"channel": "ops-alerts"}, "risk": "medium",
        "payload_template": PAYLOAD_TEMPLATE,
    })
    sent: list[dict] = []
    reply = {"status": 200, "text": "ok", "raise": None}

    async def transport(url, payload, headers, timeout):
        sent.append({"url": url, "payload": payload, "headers": headers})
        if reply["raise"] is not None:
            raise reply["raise"]
        return reply["status"], reply["text"]

    dispatcher = ActionDispatcher(
        {"ops-alerts": {"url": CHANNEL_URL, "secret": "s3cret"}},
        transport=transport)
    service = ActionService(store, templates, dispatcher, enabled=True,
                            approval_ttl_hours=72, max_attempts=3)
    e = SimpleNamespace(store=store, templates=templates, service=service,
                        dispatcher=dispatcher, sent=sent, reply=reply)
    try:
        yield e
    finally:
        await store.dispose()


def _confirm(env):
    return env.templates.confirm("notify-ops")


def _rule(**kw):
    base = dict(
        id="revenue-drop", name="Revenue drop", severity="warning",
        priority=2, recommendation="Check the campaign calendar",
        subject=Subject(metrics=["revenue"]),
        action=ActionRef(template="notify-ops", autonomy="propose"),
    )
    base.update(kw)
    return DecisionRule(**base)


def _outcome(*, triggered=True, error="", digest="d1", anchor=ANCHOR):
    return DecisionOutcome(
        triggered=triggered, message="[warning] Revenue drop",
        rule_id="revenue-drop", severity="warning", error=error,
        evidence={
            "rule_digest": digest,
            "times": {"anchor_date": anchor,
                      "evaluated_at": f"{anchor}T09:00:00"},
            "rows": [{"dim": "north", "triggered": True, "current": 1234,
                      "baseline": 1400, "delta": -166, "delta_pct": -0.1186,
                      "contribution": -166.0}],
        },
    )


async def _propose(env, rule=None, outcome=None, **kw):
    return await env.service.propose_from_verdict(
        rule=rule or _rule(), outcome=outcome or _outcome(),
        datasource="financial", **kw)


def _advance(env, *, hours: int) -> None:
    """把服务的钟拨到未来 —— 过期是"时间到了",不是某个可改字段
    (``expires_at`` 不在 store 的可变列里,这是有意的)。"""
    later = datetime.now() + timedelta(hours=hours)
    env.service._now = staticmethod(lambda: later)


# ── propose:什么时候**不**建提案 ────────────────────────

async def test_disabled_layer_proposes_nothing(env):
    env.service.enabled = False
    assert await _propose(env) is None
    assert await env.store.list_proposals() == []


async def test_notify_only_and_actionless_rules_propose_nothing(env):
    _confirm(env)
    notify_only = _rule(action=ActionRef(template="notify-ops",
                                         autonomy="notify_only"))
    no_action = _rule(action=None)
    assert await _propose(env, rule=notify_only) is None
    assert await _propose(env, rule=no_action) is None


async def test_untriggered_or_erroring_verdicts_propose_nothing(env):
    _confirm(env)
    assert await _propose(env, outcome=_outcome(triggered=False)) is None
    assert await _propose(env, outcome=_outcome(error="boom")) is None


# ── propose:响亮失败 ────────────────────────────────────

async def test_unconfirmed_template_is_refused(env):
    with pytest.raises(ProposalError) as e:
        await _propose(env)
    assert "not confirmed" in str(e.value)
    assert await env.store.list_proposals() == []


async def test_missing_template_is_refused(env):
    rule = _rule(action=ActionRef(template="ghost", autonomy="propose"))
    with pytest.raises(ProposalError) as e:
        await _propose(env, rule=rule)
    assert "ghost" in str(e.value)


async def test_unrenderable_payload_creates_no_proposal(env):
    """admin 用 params 把值钉进非字符串位、钉成了非 JSON 字面量 → 渲染硬错,
    **不建**半成品提案(模板自己过得了 create 校验,这是运行时才发现的那类)。"""
    _confirm(env)
    rule = _rule(action=ActionRef(template="notify-ops", autonomy="propose",
                                  params={"current": "n/a"}))
    with pytest.raises(RenderError):
        await _propose(env, rule=rule)
    assert await env.store.list_proposals() == []


# ── propose:成功与去重 ──────────────────────────────────

async def test_propose_creates_a_pending_proposal(env):
    _confirm(env)
    p = await _propose(env, run_id=7, job_id="job-1", created_by="scheduler")
    assert p.status == "pending"
    assert p.template == "notify-ops"
    assert p.risk == "medium"
    assert p.payload == {"rule": "revenue-drop", "metric": "revenue",
                         "current": 1234, "msg": "[warning] Revenue drop"}
    assert "_trove" not in p.payload  # 信封只在 dispatch 时加
    assert p.evidence_refs == {"run_id": 7, "job_id": "job-1"}
    assert p.expires_at == (datetime.fromisoformat(p.created_at)
                            + timedelta(hours=72)).isoformat(timespec="seconds")
    assert (await env.store.count_by_status()) == {"pending": 1}


async def test_same_firing_dedupes_to_the_same_proposal(env):
    _confirm(env)
    a = await _propose(env)
    b = await _propose(env)
    assert a.id == b.id
    assert len(await env.store.list_proposals()) == 1


async def test_a_new_anchor_or_rule_version_proposes_again(env):
    _confirm(env)
    first = await _propose(env)
    new_day = await _propose(env, outcome=_outcome(anchor="2026-10-04"))
    edited = await _propose(env, outcome=_outcome(digest="d2"))
    assert len({first.id, new_day.id, edited.id}) == 3


# ── 审批门 ──────────────────────────────────────────────

async def test_approve_records_the_human_and_the_gate(env):
    _confirm(env)
    p = await _propose(env)
    approved = await env.service.approve(p.id, "admin", "go ahead")
    assert approved.status == "approved"
    assert approved.decided_at
    trail = await env.store.list_approvals(p.id)
    assert [(a.user_id, a.action, a.comment) for a in trail] == [
        ("admin", "approve", "go ahead")]
    with pytest.raises(ProposalError) as e:
        await env.service.approve(p.id, "admin2")
    assert "not 'pending'" in str(e.value)


async def test_reject_is_terminal(env):
    _confirm(env)
    p = await _propose(env)
    assert (await env.service.reject(p.id, "admin", "no")).status == "rejected"
    with pytest.raises(ProposalError):
        await env.service.approve(p.id, "admin")


async def test_cancel_works_before_dispatch_only(env):
    _confirm(env)
    p = await _propose(env)
    assert (await env.service.cancel(p.id, "admin")).status == "cancelled"

    q = await _propose(env, outcome=_outcome(anchor="2026-10-04"))
    await env.service.approve(q.id, "admin")
    assert (await env.service.cancel(q.id, "admin")).status == "cancelled"

    r = await _propose(env, outcome=_outcome(anchor="2026-10-05"))
    await env.service.approve(r.id, "admin")
    await env.service.dispatch(r.id, "admin")
    with pytest.raises(ProposalError) as e:
        await env.service.cancel(r.id, "admin")
    assert "left the building" in str(e.value)


async def test_approving_past_the_deadline_expires_instead(env):
    """清收任务可能还没跑 —— 审批这道闸自己也要挡住过期件。"""
    _confirm(env)
    p = await _propose(env)
    _advance(env, hours=73)  # ttl = 72h
    with pytest.raises(ProposalError) as e:
        await env.service.approve(p.id, "admin")
    assert "expired" in str(e.value)
    assert (await env.store.get_proposal(p.id)).status == "expired"


# ── 外送 ────────────────────────────────────────────────

async def test_dispatch_sends_the_frozen_payload_with_the_envelope(env):
    _confirm(env)
    p = await _propose(env)
    await env.service.approve(p.id, "admin")
    done = await env.service.dispatch(p.id, "admin")

    assert done.status == "dispatched"
    assert done.attempts == 1
    assert done.dispatched_at
    assert len(env.sent) == 1
    call = env.sent[0]
    assert call["url"] == CHANNEL_URL
    assert call["headers"]["Authorization"] == "Bearer s3cret"
    assert call["payload"]["rule"] == "revenue-drop"       # 冻结的模板载荷
    envelope = call["payload"]["_trove"]                   # + 身份信封
    assert envelope["proposal_id"] == p.id
    assert envelope["idempotency_key"] == p.idempotency_key
    assert envelope["approved_by"] == "admin"
    assert envelope["datasource"] == "financial"

    receipts = await env.store.list_deliveries(p.id)
    assert [(d.channel, d.status, d.http_status) for d in receipts] == [
        ("ops-alerts", "sent", 200)]
    trail = await env.store.list_approvals(p.id)
    assert [a.action for a in trail] == ["approve", "dispatch"]


async def test_dispatch_is_refused_while_disabled(env):
    _confirm(env)
    p = await _propose(env)
    await env.service.approve(p.id, "admin")
    env.service.enabled = False
    with pytest.raises(ProposalError) as e:
        await env.service.dispatch(p.id, "admin")
    assert "disabled" in str(e.value)
    assert env.sent == []
    assert (await env.store.get_proposal(p.id)).status == "approved"


async def test_only_approved_can_be_dispatched(env):
    _confirm(env)
    p = await _propose(env)
    with pytest.raises(ProposalError) as e:
        await env.service.dispatch(p.id, "admin")
    assert "approved" in str(e.value)


async def test_http_error_lands_as_a_failed_proposal_with_a_receipt(env):
    """行动失败必须响亮:状态 failed + 回执行 + 原始错误,下个 tick 不会
    再来一次。"""
    _confirm(env)
    p = await _propose(env)
    await env.service.approve(p.id, "admin")
    env.reply.update(status=500, text="boom: quota exceeded")

    failed = await env.service.dispatch(p.id, "admin")
    assert failed.status == "failed"
    assert failed.error == "HTTP 500"
    receipts = await env.store.list_deliveries(p.id)
    assert receipts[0].status == "failed"
    assert receipts[0].http_status == 500
    assert receipts[0].response_excerpt == "boom: quota exceeded"


async def test_transport_exception_lands_as_a_failed_proposal(env):
    _confirm(env)
    p = await _propose(env)
    await env.service.approve(p.id, "admin")
    env.reply["raise"] = TimeoutError("connect timed out")

    failed = await env.service.dispatch(p.id, "admin")
    assert failed.status == "failed"
    assert "TimeoutError" in failed.error
    assert (await env.store.list_deliveries(p.id))[0].status == "failed"


async def test_unknown_channel_fails_the_proposal_not_the_process(env):
    env.templates.create({
        "name": "misdirected", "title": "Misdirected",
        "target": {"channel": "ghost-channel"},
        "payload_template": '{"rule": "{{rule_id}}"}',
    })
    env.templates.confirm("misdirected")
    rule = _rule(action=ActionRef(template="misdirected", autonomy="propose"))
    p = await _propose(env, rule=rule)
    await env.service.approve(p.id, "admin")
    failed = await env.service.dispatch(p.id, "admin")
    assert failed.status == "failed"
    assert "not configured" in failed.error


async def test_retry_is_an_explicit_second_attempt(env):
    _confirm(env)
    p = await _propose(env)
    await env.service.approve(p.id, "admin")
    env.reply["status"] = 503
    await env.service.dispatch(p.id, "admin")

    env.reply["status"] = 200
    done = await env.service.retry(p.id, "admin")
    assert done.status == "dispatched"
    assert done.attempts == 2
    assert done.error == ""
    assert [a.action for a in await env.store.list_approvals(p.id)] == [
        "approve", "dispatch", "retry"]
    assert len(env.sent) == 2


async def test_attempts_are_capped(env):
    env.service.max_attempts = 1
    _confirm(env)
    p = await _propose(env)
    await env.service.approve(p.id, "admin")
    env.reply["status"] = 500
    await env.service.dispatch(p.id, "admin")
    with pytest.raises(ProposalError) as e:
        await env.service.retry(p.id, "admin")
    assert "attempts" in str(e.value)
    assert len(env.sent) == 1


async def test_retry_requires_a_failed_proposal(env):
    _confirm(env)
    p = await _propose(env)
    await env.service.approve(p.id, "admin")
    with pytest.raises(ProposalError) as e:
        await env.service.retry(p.id, "admin")
    assert "failed" in str(e.value)


# ── 回执 / 清收 / 读 ────────────────────────────────────

async def test_ack_closes_the_loop_and_is_idempotent(env):
    _confirm(env)
    p = await _propose(env)
    await env.service.approve(p.id, "admin")
    delivered = await env.service.ack(p.id, "admin", "seen in ops")
    assert delivered.status == "delivered"
    receipts = await env.store.list_deliveries(p.id)
    assert [(d.channel, d.status) for d in receipts] == [("ack", "ack")]
    assert receipts[0].response_excerpt == "seen in ops"

    again = await env.service.ack(p.id, "admin", "seen in ops")
    assert again.status == "delivered"
    assert len(await env.store.list_deliveries(p.id)) == 1  # 不重复落回执


async def test_ack_needs_something_that_actually_went_out(env):
    _confirm(env)
    p = await _propose(env)
    with pytest.raises(ProposalError) as e:
        await env.service.ack(p.id, "admin")
    assert "acked" in str(e.value)


async def test_expire_due_sweeps_only_overdue_pending(env):
    _confirm(env)
    p = await _propose(env)
    q = await _propose(env, outcome=_outcome(anchor="2026-10-04"))
    await env.service.approve(q.id, "admin")
    _advance(env, hours=73)

    assert await env.service.expire_due() == 1
    assert (await env.store.get_proposal(p.id)).status == "expired"
    assert (await env.store.get_proposal(q.id)).status == "approved"


async def test_get_returns_the_trail_and_flags_stale(env):
    _confirm(env)
    p = await _propose(env)
    detail = await env.service.get(p.id)
    assert detail["proposal"].id == p.id
    assert detail["approvals"] == [] and detail["deliveries"] == []
    assert detail["stale"] is False

    _advance(env, hours=73)
    assert (await env.service.get(p.id))["stale"] is True
    assert await env.service.get("nope") is None


async def test_status_counts_and_open_statuses_contract(env):
    """治理待办的 open 口径 = 三个在等人/等重试的状态 —— 管理台计数就
    建在这上面,换常量先在这里破防。"""
    _confirm(env)
    p = await _propose(env)
    await env.service.approve(p.id, "admin")
    assert await env.service.status_counts() == {"approved": 1}
    assert OPEN_STATUSES == ("pending", "approved", "failed")
