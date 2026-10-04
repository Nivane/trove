"""B5 行动基建:护栏 / 失败分类 / 重试退避 / dry-run(零网络、零 LLM)。

三条设计线在这一组测试里各自钉住:

- **默认档逐字节不变** —— 护栏全默认关时,max_risk 放行一切、rate_limit 不
  计数、退避 base=0 让 `retry_due_proposals` 连库都不查。
- **失败分类决定重试** —— 5xx/429/超时重试有意义,4xx 与配置错没有;同一
  个分类器既判"刚发生的失败",也判"库里存着的失败"(重试扫描器)。
- **重试时点是纯派生** —— `(attempts, 最近一次真实投递的 attempted_at)`,
  零新列;预演行与 ack 行都不参与这个时钟。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from trove.services.action.dispatcher import (
    ActionDispatcher,
    DispatchResult,
    classify_failure,
)
from trove.services.action.guard import (
    ActionGuards,
    backoff_delay,
    channel_rate_ok,
    next_attempt_at,
    risk_allowed,
)
from trove.services.action.models import Delivery
from trove.services.action.propose import ProposalError
from trove.services.action.service import ActionService
from trove.services.action.store import ActionStore
from trove.services.action.templates import ActionTemplateService
from trove.services.decision.rules import ActionRef, DecisionRule, Subject
from trove.services.decision.service import DecisionOutcome

CHANNEL_URL = "https://hook.invalid/ops"
PAYLOAD_TEMPLATE = ('{"rule": "{{rule_id}}", "metric": "{{metric}}", '
                    '"current": {{current}}, "msg": "{{message}}"}')
ANCHOR = "2026-10-03"
NOW = datetime(2026, 10, 3, 9, 0, 0)


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
    templates.confirm("notify-ops")
    sent: list[dict] = []
    reply = {"status": 200, "text": "ok", "raise": None, "retry_after": None}

    async def transport(url, payload, headers, timeout):
        sent.append({"url": url, "payload": payload})
        if reply["raise"] is not None:
            raise reply["raise"]
        if reply["retry_after"] is not None:
            return reply["status"], reply["text"], reply["retry_after"]
        return reply["status"], reply["text"]

    dispatcher = ActionDispatcher(
        {"ops-alerts": {"url": CHANNEL_URL, "secret": "s3cret"}},
        transport=transport)
    service = ActionService(store, templates, dispatcher, enabled=True,
                            max_attempts=3)
    e = SimpleNamespace(store=store, templates=templates, service=service,
                        dispatcher=dispatcher, sent=sent, reply=reply,
                        clock=[datetime.now()])
    service._now = lambda: e.clock[0]

    def advance(**kw):
        e.clock[0] = e.clock[0] + timedelta(**kw)

    e.advance = advance
    try:
        yield e
    finally:
        await store.dispose()


def _rule(**kw):
    base = dict(
        id="revenue-drop", name="Revenue drop", severity="warning",
        priority=2, recommendation="Check the campaign calendar",
        subject=Subject(metrics=["revenue"]),
        action=ActionRef(template="notify-ops", autonomy="propose"),
    )
    base.update(kw)
    return DecisionRule(**base)


def _outcome(*, anchor=ANCHOR, digest="d1"):
    return DecisionOutcome(
        triggered=True, message="[warning] Revenue drop",
        rule_id="revenue-drop", severity="warning", error="",
        evidence={
            "rule_digest": digest,
            "times": {"anchor_date": anchor,
                      "evaluated_at": f"{anchor}T09:00:00"},
            "rows": [{"dim": "north", "triggered": True, "current": 1234,
                      "baseline": 1400, "delta": -166, "delta_pct": -0.1186,
                      "contribution": -166.0}],
        },
    )


async def _proposal(env, *, anchor=ANCHOR, digest="d1"):
    return await env.service.propose_from_verdict(
        rule=_rule(), outcome=_outcome(anchor=anchor, digest=digest),
        datasource="financial")


async def _approved(env, **kw):
    p = await _proposal(env, **kw)
    return await env.service.approve(p.id, "admin", "go")


def _fail_dispatch(env, *, status=500, text="boom", **kw):
    env.reply.update({"status": status, "text": text})
    return kw


# ── 纯函数:风险上限 ─────────────────────────────────────

def test_risk_ceiling_default_allows_everything():
    assert risk_allowed("high", "high") is True
    assert risk_allowed("low", "high") is True


def test_risk_ceiling_blocks_above_and_unknown_labels():
    assert risk_allowed("medium", "low") is False
    assert risk_allowed("high", "low") is False
    assert risk_allowed("low", "low") is True
    # 有天花板时,认不出的风险不放行;没天花板时未知不是护栏。
    assert risk_allowed("weird", "medium") is False
    assert risk_allowed("weird", "nonsense") is True


# ── 纯函数:通道速率 ─────────────────────────────────────

def _stamps(*offsets_s: int, now: datetime) -> list[str]:
    return [(now - timedelta(seconds=s)).isoformat(timespec="seconds")
            for s in offsets_s]


def test_rate_limit_off_by_default():
    assert channel_rate_ok([], now=NOW, limit=0) is True
    assert channel_rate_ok(_stamps(1, 2, 3, now=NOW), now=NOW, limit=0) is True


def test_rate_limit_counts_only_within_the_window():
    times = _stamps(1, 30, 59, 61, 600, now=NOW)
    assert channel_rate_ok(times, now=NOW, limit=4, window_s=60) is True
    assert channel_rate_ok(times, now=NOW, limit=3, window_s=60) is False


def test_rate_limit_ignores_unparseable_stamps():
    assert channel_rate_ok(["not-a-time"], now=NOW, limit=1, window_s=60) is True


# ── 纯函数:退避 ────────────────────────────────────────

def test_backoff_is_off_without_a_base():
    assert backoff_delay(3, base_s=0) == 0
    assert next_attempt_at(3, NOW.isoformat(), base_s=0) is None


def test_backoff_is_exponential_and_capped():
    assert backoff_delay(1, base_s=30) == 30
    assert backoff_delay(2, base_s=30) == 60
    assert backoff_delay(3, base_s=30) == 120
    assert backoff_delay(10, base_s=30, max_s=300) == 300


def test_next_attempt_at_is_a_pure_derivation():
    last = NOW.isoformat(timespec="seconds")
    assert next_attempt_at(1, last, base_s=60) == NOW + timedelta(seconds=60)
    assert next_attempt_at(2, last, base_s=60) == NOW + timedelta(seconds=120)
    # 不可依赖的上次尝试 → 没有时点,绝不返回一个"看起来安全"的假时点。
    assert next_attempt_at(1, "", base_s=60) is None
    assert next_attempt_at(1, "yesterday", base_s=60) is None


# ── ActionGuards.from_config ────────────────────────────

def test_guards_from_config_defaults_are_all_off():
    assert ActionGuards.from_config(None) == ActionGuards()


def test_guards_from_config_reads_the_action_config():
    cfg = SimpleNamespace(max_risk="LOW", rate_limit="5",
                          retry_backoff_base_s="30", retry_backoff_factor="3",
                          retry_backoff_max_s="600")
    g = ActionGuards.from_config(cfg)
    assert (g.max_risk, g.rate_limit, g.retry_backoff_base_s,
            g.retry_backoff_factor, g.retry_backoff_max_s) == \
        ("low", 5, 30, 3.0, 600)


# ── 失败分类:决定重试有没有意义 ────────────────────────

def test_transient_failures_are_retryable():
    assert classify_failure(500, "HTTP 500", "boom").retryable is True
    assert classify_failure(503, "HTTP 503").retryable is True
    assert classify_failure(429, "HTTP 429").retryable is True
    assert classify_failure(None, "ConnectError: connection refused").retryable
    assert classify_failure(None, "ReadTimeout: timed out").retryable


def test_config_and_client_failures_are_not_retryable():
    assert classify_failure(404, "HTTP 404").retryable is False
    assert classify_failure(401, "HTTP 401").retryable is False
    assert classify_failure(
        None, "channel 'ops' is not configured (configured channels: none)"
    ).retryable is False
    assert classify_failure(
        None, "channel 'ops' has no usable url").retryable is False
    assert classify_failure(None, "unknown channel kind 'matrix'").retryable is False


def test_explicit_tag_wins_over_wording():
    """护栏拒绝写的是带标签的文本 —— 扫描器只凭存下来的文本就能重建判定。"""
    verdict = classify_failure(
        None, "[ERR:ACTION_CONFIG] guardrail: proposal risk 'high' exceeds 'low'")
    assert verdict.tag() == "[ERR:ACTION_CONFIG]"
    assert verdict.retryable is False
    limited = classify_failure(
        None, "[ERR:ACTION_RATE_LIMITED] guardrail: channel 'ops' exhausted")
    assert limited.retryable is True


# ── 分发器:retryable / retry_after / preflight ─────────

async def test_dispatch_result_carries_retryability():
    calls: list = []

    async def transport(url, payload, headers, timeout):
        return calls[-1]

    dispatcher = ActionDispatcher({"ops": {"url": "https://x.invalid/h"}},
                                  transport=transport)
    calls.append((500, "internal server error"))
    assert (await dispatcher.send("ops", {})).retryable is True
    calls.append((404, "not found"))
    assert (await dispatcher.send("ops", {})).retryable is False
    calls.append((200, "ok"))
    ok = await dispatcher.send("ops", {})
    assert ok.ok and ok.retryable is False


async def test_retry_after_is_reported_but_never_on_success():
    async def transport(url, payload, headers, timeout):
        return 429, "slow down", 42.5

    dispatcher = ActionDispatcher({"ops": {"url": "https://x.invalid/h"}},
                                  transport=transport)
    result = await dispatcher.send("ops", {})
    assert result.ok is False and result.retryable is True
    assert result.retry_after_s == 42.5

    async def ok_transport(url, payload, headers, timeout):
        return 200, "ok", 42.5

    dispatcher2 = ActionDispatcher({"ops": {"url": "https://x.invalid/h"}},
                                   transport=ok_transport)
    assert (await dispatcher2.send("ops", {})).retry_after_s is None


async def test_two_value_transports_still_work():
    async def transport(url, payload, headers, timeout):
        return 200, "ok"

    dispatcher = ActionDispatcher({"ops": {"url": "https://x.invalid/h"}},
                                  transport=transport)
    assert (await dispatcher.send("ops", {})).ok is True


async def test_channel_kind_shapes_the_body():
    seen: list[dict] = []

    async def transport(url, payload, headers, timeout):
        seen.append(payload)
        return 200, "ok"

    dispatcher = ActionDispatcher(
        {"ops": {"url": "https://x.invalid/h", "kind": "slack"}},
        transport=transport)
    await dispatcher.send("ops", {"rule_id": "r1", "message": "m"})
    assert seen[0]["blocks"][0]["text"]["text"] == "rule_id: r1\nmessage: m"


async def test_unknown_channel_kind_fails_the_dispatch_not_the_process():
    calls: list = []

    async def transport(url, payload, headers, timeout):
        calls.append(payload)
        return 200, "ok"

    dispatcher = ActionDispatcher(
        {"ops": {"url": "https://x.invalid/h", "kind": "matrix"}},
        transport=transport)
    result = await dispatcher.send("ops", {"a": 1})
    assert result.ok is False
    assert "unknown channel kind" in result.error
    assert calls == [] and result.retryable is False


def test_preflight_judges_what_send_judges_without_sending():
    dispatcher = ActionDispatcher({"ops": {"url": "https://x.invalid/h"}})
    assert dispatcher.preflight("ops", {"a": 1}).ok is True
    assert dispatcher.preflight("ghost", {"a": 1}).ok is False
    assert dispatcher.preflight("", {"a": 1}).ok is False


def test_preflight_matches_send_on_a_bad_kind():
    dispatcher = ActionDispatcher(
        {"ops": {"url": "https://x.invalid/h", "kind": "matrix"}})
    pre = dispatcher.preflight("ops", {"a": 1})
    assert pre.ok is False and "unknown channel kind" in pre.error


# ── 服务:dry-run ────────────────────────────────────────

async def test_dry_run_leaves_the_proposal_untouched(env):
    p = await _proposal(env)
    fresh = await env.service.dry_run(p.id, "admin", "preview")
    assert fresh.status == "pending", "预演不改状态"
    assert fresh.attempts == 0, "预演不消耗尝试次数"
    assert env.sent == [], "预演不发 POST"

    detail = await env.service.get(p.id)
    assert [a.action for a in detail["approvals"]] == ["dry_run"]
    deliv = detail["deliveries"][-1]
    assert deliv.status == "dry_run"
    assert deliv.http_status is None
    assert "revenue-drop" in deliv.response_excerpt, "存的是本来会发出去的那一份"
    assert deliv.error == ""


async def test_dry_run_reports_a_broken_channel_as_a_receipt_row(env):
    p = await _proposal(env)
    env.dispatcher.channels.pop("ops-alerts")
    fresh = await env.service.dry_run(p.id, "admin")
    assert fresh.status == "pending"
    detail = await env.service.get(p.id)
    deliv = detail["deliveries"][-1]
    assert deliv.status == "dry_run"
    assert "not configured" in deliv.error
    assert env.sent == []


async def test_dry_run_is_allowed_while_the_layer_is_disabled(env):
    """预演是"看",不是"发" —— 与模板预览同档:关着也能用。"""
    p = await _proposal(env)
    env.service.enabled = False
    fresh = await env.service.dry_run(p.id, "admin")
    assert fresh.status == "pending"
    assert (await env.store.list_deliveries(p.id))[-1].status == "dry_run"


async def test_dry_run_is_refused_on_a_closed_proposal(env):
    p = await _proposal(env)
    await env.service.reject(p.id, "admin", "no")
    with pytest.raises(ProposalError, match="dry-run"):
        await env.service.dry_run(p.id, "admin")
    assert [d.status for d in await env.store.list_deliveries(p.id)] == []


async def test_dry_run_does_not_turn_the_first_dispatch_into_a_retry(env):
    """预演行不许污染审批轨迹:它之后的第一次真外送仍是 dispatch。"""
    p = await _proposal(env)
    await env.service.dry_run(p.id, "admin")
    await env.service.approve(p.id, "admin")
    fresh = await env.service.dispatch(p.id, "admin")
    assert fresh.status == "dispatched" and fresh.attempts == 1
    actions = [a.action for a in await env.store.list_approvals(p.id)]
    assert actions == ["dry_run", "approve", "dispatch"]


# ── 服务:护栏门 ─────────────────────────────────────────

async def test_risk_ceiling_refuses_and_records_instead_of_silence(env):
    env.service.guards = ActionGuards(max_risk="low")   # 模板 risk=medium
    p = await _approved(env)
    with pytest.raises(ProposalError, match="guardrail"):
        await env.service.dispatch(p.id, "admin")

    fresh = await env.store.get_proposal(p.id)
    assert fresh.status == "failed"
    assert fresh.error.startswith("[ERR:ACTION_CONFIG] guardrail: ")
    assert fresh.attempts == 0, "被拒不是一次尝试"
    assert env.sent == [], "被拒必须真的没发出去"
    assert await env.store.list_deliveries(p.id) == [], "没有投递行"
    # 存下来的文本可以重建"不该重试"这个判定(重试扫描器只读库)。
    assert classify_failure(None, fresh.error).retryable is False


async def test_rate_limit_refuses_the_second_send_in_the_window(env):
    env.service.guards = ActionGuards(rate_limit=1)
    first = await _approved(env)
    assert (await env.service.dispatch(first.id, "admin")).status == "dispatched"

    second = await _approved(env, anchor="2026-10-04", digest="d2")
    with pytest.raises(ProposalError, match="guardrail"):
        await env.service.dispatch(second.id, "admin")

    fresh = await env.store.get_proposal(second.id)
    assert fresh.status == "failed"
    assert fresh.error.startswith("[ERR:ACTION_RATE_LIMITED] guardrail: ")
    assert len(env.sent) == 1, "第二次真的没发出去"

    # 窗口过去 + 退避走开之后,它是**可重试**的(429 的语义)。
    env.advance(seconds=61)
    assert classify_failure(None, fresh.error).retryable is True


async def test_guards_never_touch_the_default_path(env):
    """全关的默认档:三次失败重试与护栏存在之前逐字节一致。"""
    p = await _approved(env)
    env.reply.update({"status": 500, "text": "boom"})
    assert (await env.service.dispatch(p.id, "admin")).status == "failed"
    env.reply.update({"status": 200, "text": "ok"})
    fresh = await env.service.retry(p.id, "admin")
    assert fresh.status == "dispatched" and fresh.attempts == 2


# ── 服务:自动重试到点 ──────────────────────────────────

async def test_auto_retry_is_off_by_default(env):
    p = await _approved(env)
    env.reply.update({"status": 500, "text": "boom"})
    await env.service.dispatch(p.id, "admin")
    env.advance(hours=1)
    assert await env.service.retry_due_proposals() == 0
    assert len(env.sent) == 1
    assert (await env.store.get_proposal(p.id)).attempts == 1


async def test_auto_retry_fires_only_after_the_backoff_elapsed(env):
    env.service.guards = ActionGuards(retry_backoff_base_s=60)
    p = await _approved(env)
    env.reply.update({"status": 500, "text": "boom"})
    await env.service.dispatch(p.id, "admin")

    assert await env.service.retry_due_proposals() == 0, "退避没到,不动"
    env.advance(seconds=61)
    env.reply.update({"status": 200, "text": "ok"})
    assert await env.service.retry_due_proposals() == 1
    fresh = await env.store.get_proposal(p.id)
    assert fresh.status == "dispatched" and fresh.attempts == 2
    assert len(env.sent) == 2


async def test_auto_retry_gives_up_on_non_retryable_failures(env):
    """同一份错配置重试一万次还是同一份错配置。"""
    env.service.guards = ActionGuards(retry_backoff_base_s=60)
    p = await _approved(env)
    env.reply.update({"status": 404, "text": "not found"})
    await env.service.dispatch(p.id, "admin")
    env.advance(hours=1)
    assert await env.service.retry_due_proposals() == 0
    assert len(env.sent) == 1


async def test_auto_retry_respects_max_attempts(env):
    env.service.guards = ActionGuards(retry_backoff_base_s=60)
    env.service.max_attempts = 1
    p = await _approved(env)
    env.reply.update({"status": 500, "text": "boom"})
    await env.service.dispatch(p.id, "admin")
    env.advance(hours=1)
    assert await env.service.retry_due_proposals() == 0


async def test_auto_retry_skips_a_proposal_with_no_real_attempt(env):
    """没有真实投递行 = 没有时钟可依赖(绝不拿预演行当时钟)。"""
    env.service.guards = ActionGuards(retry_backoff_base_s=1)
    p = await _proposal(env)
    await env.service.dry_run(p.id, "admin")
    await env.store.update_proposal(p.id, status="failed",
                                    error="HTTP 503 boom")
    env.advance(hours=1)
    assert await env.service.retry_due_proposals() == 0
    assert env.sent == []


async def test_auto_retry_uses_the_real_receipt_not_a_dry_run_clock(env):
    env.service.guards = ActionGuards(retry_backoff_base_s=60)
    p = await _proposal(env)
    await env.service.dry_run(p.id, "admin")
    await env.service.approve(p.id, "admin")
    env.reply.update({"status": 500, "text": "boom"})
    await env.service.dispatch(p.id, "admin")
    env.advance(seconds=61)
    env.reply.update({"status": 200, "text": "ok"})
    assert await env.service.retry_due_proposals() == 1


# ── store:速率护栏的事实源 ──────────────────────────────

async def test_delivery_times_count_only_real_attempts(env):
    p = await _proposal(env)
    await env.service.dry_run(p.id, "admin")          # 不算
    await env.service.approve(p.id, "admin")
    env.reply.update({"status": 500, "text": "boom"})
    await env.service.dispatch(p.id, "admin")         # failed,算
    env.reply.update({"status": 200, "text": "ok"})
    await env.service.dispatch(p.id, "admin")         # sent,算
    await env.service.ack(p.id, "admin")              # ack,不算

    times = await env.store.list_delivery_times("ops-alerts", "2000-01-01")
    assert len(times) == 2
    assert times == sorted(times)


async def test_delivery_times_filter_by_channel_and_window(env):
    p = await _proposal(env)
    await env.service.approve(p.id, "admin")
    await env.service.dispatch(p.id, "admin")
    assert await env.store.list_delivery_times("other", "2000-01-01") == []
    assert await env.store.list_delivery_times("ops-alerts", "2999-01-01") == []


async def test_store_helper_ignores_dry_run_rows_directly(env):
    await env.store.add_delivery(Delivery(
        proposal_id="p", channel="ops-alerts", status="dry_run",
        attempted_at="2026-10-03T09:00:00"))
    await env.store.add_delivery(Delivery(
        proposal_id="p", channel="ops-alerts", status="sent",
        attempted_at="2026-10-03T09:01:00"))
    assert await env.store.list_delivery_times(
        "ops-alerts", "2000-01-01") == ["2026-10-03T09:01:00"]


# ── DispatchResult 形状(契约)────────────────────────────

def test_dispatch_result_defaults_are_backward_compatible():
    r = DispatchResult(ok=True, channel="c")
    assert r.retryable is False and r.retry_after_s is None
