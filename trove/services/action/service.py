"""``ActionService`` — propose → approve → dispatch → receipt, gated twice.

The whole pillar in one state machine. Two independent gates stand between a
fired rule and an outbound message:

1. **The template gate** (``templates.py``): a template is a pending draft
   until an admin confirms it, and ``decision/rules.py`` refuses to save a
   rule referencing an unconfirmed one.
2. **The approval gate** (here): a proposal is created ``pending`` and only a
   human ``approve`` moves it to ``approved``, which is the only state from
   which ``dispatch`` may run.

**Disabled means both.** ``agent.action.enabled`` defaults to ``false`` — and
when it is off, *propose* and *dispatch* are both refused (the two directions
that reach the outside). Approve / reject / cancel / expire / ack and
template management keep working, so a deployment can stage templates and
clear out an old backlog without arming the outbound path. Dispatch under a
disabled layer raises rather than silently doing nothing: this is the one
place where "nothing happened" and "the message was sent" must never be
confusable.

**Failure is loud, unlike ``jobs/notify.py``.** The jobs alert path swallows a
webhook failure, and that is correct there: alerts repeat, so the next tick
re-delivers. An **action** does not repeat — it was approved once, against one
firing — so a failed dispatch sets ``status='failed'``, writes a delivery row
with the error, and surfaces in the admin to-do list. A second attempt is an
explicit ``retry``, bounded by ``max_attempts``.

**Bounded recovery (B5).** Three additions, all default-off so an unconfigured
deployment keeps every byte of the old behaviour:

- **Guardrails** (``guard.py``): ``max_risk`` and ``rate_limit`` are checked at
  the dispatch entry. A refusal is *recorded* — the proposal goes ``failed``
  with a tagged reason — and then raised, so the caller sees it too.
- **Automatic retry** (``retry_due_proposals``): when ``retry_backoff_*`` is
  configured, a failed-but-retryable dispatch is re-attempted once its backoff
  has elapsed. The due time is a pure derivation from ``(attempts,
  deliveries.attempted_at)`` — the retry clock is recomputable from the receipt
  trail, so it needs no column of its own and cannot drift.
- **``dry_run``**: everything a dispatch does *except* the POST — channel
  resolution, payload shaping, guardrail check — recorded as a
  ``delivery(status="dry_run")`` without migrating the proposal (the
  ``MaintenanceService.preview`` precedent: a preview that changes state is a
  preview nobody will trust twice).

**No connectors, by construction.** The constructor takes a store, a template
service, a dispatcher and config — nothing that can reach a business
datasource. The read-only posture guard test asserts this against the source.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from trove.core.logging import get_logger
from trove.services.action.dispatcher import ActionDispatcher, classify_failure
from trove.services.action.guard import (
    RATE_WINDOW_S,
    ActionGuards,
    channel_rate_ok,
    next_attempt_at,
    risk_allowed,
)
from trove.services.action.models import (
    OPEN_STATUSES,
    ActionProposal,
    Approval,
    Delivery,
)
from trove.services.action.propose import (
    ProposalError,
    anchor_of,
    build_proposal,
    proposal_key,
)
logger = get_logger(__name__)

#: 护栏拒绝写进 ``proposal.error`` / ``delivery.error`` 的固定前缀。标签
#: (``[ERR:...]``)让**重试扫描器**能从存下来的文本里重建"这次该不该再试"——
#: 判定依据是标签,不是措辞匹配。
GUARDRAIL_PREFIX = "guardrail: "

#: 只有真正发生过外送的投递行算"一次尝试";预演什么都没发。
_REAL_DELIVERY_STATUSES = ("sent", "failed")

#: ``dry_run`` 允许的提案状态:到 dispatch 还走得通的三个 + pending(审批人
#: 想在按下批准之前先看看**真实载荷**长什么样 —— 这是预演最有价值的时机)。
_DRY_RUNNABLE = ("pending", "approved", "failed")

#: Where a proposal can be when ack arrives. ``approved`` counts because the
#: MCP pull channel *is* a delivery: a client that fetched an approved
#: proposal and acked it has received it, whether or not the webhook push
#: ever ran.
_ACKABLE = ("approved", "dispatched", "delivered")


def _last_real_attempt(deliveries: list[Delivery]) -> Delivery | None:
    """最近一次**真实**投递(``sent`` / ``failed``),没有则 ``None``。

    退避时钟只认真实尝试:预演行(dry_run)不是"试过一次",ack 是回执
    也不是 —— 它们都进不了这个列表。
    """
    real = [d for d in deliveries if d.status in _REAL_DELIVERY_STATUSES]
    return real[-1] if real else None


class ActionService:
    """Proposal lifecycle for one project tree. Zero LLM, zero connectors."""

    def __init__(
        self, store, templates, dispatcher: ActionDispatcher, *,
        enabled: bool = False, approval_ttl_hours: int = 72,
        max_payload_bytes: int = 8192, max_attempts: int = 3,
        lang: str = "zh",
    ):
        self.store = store
        self.templates = templates
        self.dispatcher = dispatcher
        self.enabled = bool(enabled)
        self.approval_ttl_hours = max(1, int(approval_ttl_hours or 72))
        self.max_payload_bytes = max(0, int(max_payload_bytes or 0))
        self.max_attempts = max(1, int(max_attempts or 1))
        self.lang = lang
        #: 护栏配置。构造签名是姿态守卫钉死的(不得新增构造参数),所以护栏
        #: 走**运行时绑定**:装配点(main.py)用 ``ActionGuards.from_config``
        #: 显式绑定。默认 ActionGuards = 三道护栏全关 → 老路径逐字节不变。
        self.guards = ActionGuards()

    # ── helpers ──────────────────────────────────────────

    @staticmethod
    def _now() -> datetime:
        return datetime.now()

    async def _proposal_or_raise(self, proposal_id: str) -> ActionProposal:
        p = await self.store.get_proposal(proposal_id)
        if p is None:
            raise KeyError(f"proposal not found: {proposal_id}")
        return p

    async def _decide(
        self, p: ActionProposal, status: str, user_id: str, action: str,
        comment: str = "", **fields: Any,
    ) -> ActionProposal:
        """Record the human decision, then move the status (one audit row each)."""
        now = self._now().isoformat(timespec="seconds")
        await self.store.add_approval(Approval(
            proposal_id=p.id, user_id=str(user_id or ""), action=action,
            comment=str(comment or ""), created_at=now,
        ))
        await self.store.update_proposal(
            p.id, status=status,
            decided_at=fields.pop("decided_at", now), **fields)
        fresh = await self.store.get_proposal(p.id)
        return fresh or p

    # ── propose ──────────────────────────────────────────

    async def propose_from_verdict(
        self, *, rule: Any, outcome: Any, datasource: str,
        job_id: str = "", run_id: int | None = None,
        created_by: str = "system",
        evidence_refs: dict[str, Any] | None = None,
    ) -> ActionProposal | None:
        """A fired verdict → a pending proposal (or ``None`` — see below).

        ``None`` is returned — deliberately, not as an error — for every case
        that means "no proposal is the correct outcome": the action layer is
        disabled, the rule declares no action, ``autonomy`` is ``notify_only``
        (the rule's response is the alert itself), the verdict did not trigger,
        it carries an error, or this exact firing already produced a proposal
        (idempotent re-run).

        Everything else is a **loud failure** and raises ``ProposalError``:
        an unconfirmed/missing template, an unrenderable payload, params that
        name variables that do not exist. The caller (the scheduler runner)
        logs it and moves on — but no half-built proposal is ever created,
        and no proposal means no outbound message.
        """
        if not self.enabled:
            logger.debug("action layer disabled — no proposal for rule %s",
                         getattr(rule, "id", "?"))
            return None
        action = getattr(rule, "action", None)
        if action is None or not action.template:
            return None
        if action.autonomy != "propose":
            # notify_only: the rule's response is the notification itself.
            return None
        if not getattr(outcome, "triggered", False) or getattr(outcome, "error", ""):
            return None

        template = self.templates.get(action.template)
        if template is None:
            raise ProposalError(
                f"rule {rule.id!r}: action.template {action.template!r} is not "
                "a usable template (missing, unparseable, or invalid)")
        if template.status != "confirmed":
            raise ProposalError(
                f"rule {rule.id!r}: action.template {action.template!r} is "
                f"{template.status!r}, not confirmed — an admin must confirm "
                "it before anything can be proposed from it")

        # Dedup *before* rendering: a re-run of the same firing must be a
        # cheap no-op, not a fresh render whose failure would look like a
        # broken template.
        key = proposal_key(rule, outcome, template.name, self._now())
        existing = await self.store.find_by_idempotency_key(key)
        if existing is not None:
            logger.info(
                "action proposal deduped: rule %s anchor %s already proposed "
                "as %s", rule.id, anchor_of(outcome, self._now()), existing.id)
            return existing

        refs = dict(evidence_refs or {})
        if run_id is not None:
            refs.setdefault("run_id", run_id)
        if job_id:
            refs.setdefault("job_id", job_id)
        proposal = build_proposal(
            rule=rule, outcome=outcome, datasource=datasource,
            template=template, run_id=run_id, job_id=job_id,
            created_by=created_by, ttl_hours=self.approval_ttl_hours,
            max_payload_bytes=self.max_payload_bytes,
            evidence_refs=refs, now=self._now(),
        )
        try:
            await self.store.create_proposal(proposal)
        except Exception:
            # Two ticks racing on the same key: the unique index refuses the
            # second insert, and the correct answer is the row that won.
            winner = await self.store.find_by_idempotency_key(proposal.idempotency_key)
            if winner is not None:
                logger.info("action proposal race resolved onto %s", winner.id)
                return winner
            raise
        logger.info("action proposal %s created (rule %s, template %s)",
                    proposal.id, proposal.rule_id, proposal.template)
        return proposal

    # ── approve / reject / cancel ────────────────────────

    async def approve(
        self, proposal_id: str, user_id: str, comment: str = "",
    ) -> ActionProposal:
        p = await self._proposal_or_raise(proposal_id)
        if p.status != "pending":
            raise ProposalError(
                f"proposal {p.id} is {p.status!r}, not 'pending' — only a "
                "pending proposal can be approved")
        if self._is_expired(p):
            # The sweep may not have run yet; approving past the deadline is
            # the one outcome an expiry exists to prevent.
            now = self._now().isoformat(timespec="seconds")
            await self.store.update_proposal(
                p.id, status="expired", decided_at=now)
            raise ProposalError(
                f"proposal {p.id} expired at {p.expires_at} and can no longer "
                "be approved")
        return await self._decide(p, "approved", user_id, "approve", comment)

    async def reject(
        self, proposal_id: str, user_id: str, comment: str = "",
    ) -> ActionProposal:
        p = await self._proposal_or_raise(proposal_id)
        if p.status != "pending":
            raise ProposalError(
                f"proposal {p.id} is {p.status!r}, not 'pending' — only a "
                "pending proposal can be rejected")
        return await self._decide(p, "rejected", user_id, "reject", comment)

    async def cancel(
        self, proposal_id: str, user_id: str, comment: str = "",
    ) -> ActionProposal:
        """Withdraw a pending or approved proposal (nothing has gone out yet)."""
        p = await self._proposal_or_raise(proposal_id)
        if p.status not in ("pending", "approved"):
            raise ProposalError(
                f"proposal {p.id} is {p.status!r} — only a pending or approved "
                "proposal can be cancelled (a dispatched one has left the "
                "building)")
        return await self._decide(p, "cancelled", user_id, "cancel", comment)

    # ── dispatch / retry / ack ───────────────────────────

    async def dispatch(
        self, proposal_id: str, user_id: str, comment: str = "",
        *, dry_run: bool = False,
    ) -> ActionProposal:
        """Send an approved proposal to its channel; retry a failed one.

        ``dry_run=True`` 是**预演**:前置(状态、通道解析、载荷塑形、护栏)
        全部真跑,唯独不发 POST,并把结论写成一行 ``delivery(status=
        "dry_run")`` —— 提案状态不动、尝试次数不涨(见 :meth:`_dry_run`)。
        """
        p = await self._proposal_or_raise(proposal_id)
        if dry_run:
            return await self._dry_run(p, user_id, comment)
        if not self.enabled:
            # Refused, not skipped: dispatch is the one call where a silent
            # no-op would be indistinguishable from "sent".
            raise ProposalError(
                "the action layer is disabled (agent.action.enabled is false) "
                "— dispatch is refused")
        if p.status not in ("approved", "failed"):
            raise ProposalError(
                f"proposal {p.id} is {p.status!r} — only an approved (or a "
                "failed, to retry) proposal can be dispatched")
        if p.attempts >= self.max_attempts:
            raise ProposalError(
                f"proposal {p.id} already used {p.attempts} of "
                f"{self.max_attempts} dispatch attempts")
        channel = str((p.target or {}).get("channel") or "")
        # 预演行不算"发过一次":那之后的第一次真外送,审批轨迹里仍是 dispatch。
        prior = [d for d in await self.store.list_deliveries(p.id)
                 if d.status in _REAL_DELIVERY_STATUSES]
        is_retry = p.status == "failed" or bool(prior)
        now = self._now().isoformat(timespec="seconds")
        denial = await self._guard_denial(p, channel=channel)
        if denial:
            await self._refuse(p, denial)
            raise ProposalError(f"proposal {p.id} refused by {denial}")
        body = self._envelope(p, approved_by=user_id, dispatched_at=now)
        result = await self.dispatcher.send(channel, body)
        if result.retry_after_s is not None:
            # 只作观测:重试时点一律由 (attempts, deliveries) 纯派生(零新列),
            # 对面给的 Retry-After 在这里说出来给人看,不落库、不改时钟。
            logger.info("channel %s asked for a %ss retry delay",
                        channel, result.retry_after_s)

        await self.store.add_delivery(Delivery(
            proposal_id=p.id, channel=result.channel or channel,
            status="sent" if result.ok else "failed",
            http_status=result.http_status,
            response_excerpt=result.response_excerpt,
            error=result.error, attempted_at=now,
        ))
        attempts = p.attempts + 1
        if result.ok:
            await self._decide(
                p, "dispatched", user_id, "retry" if is_retry else "dispatch",
                comment, dispatched_at=now, attempts=attempts, error="")
        else:
            # Loud: the proposal is visibly failed, with the channel's answer
            # on the row. Silence here is the one outcome with no second chance.
            logger.warning(
                "action proposal %s dispatch failed: %s (retryable=%s)",
                p.id, result.error, result.retryable)
            await self._decide(
                p, "failed", user_id, "retry" if is_retry else "dispatch",
                comment, attempts=attempts, error=result.error[:300])
        fresh = await self.store.get_proposal(p.id)
        return fresh or p

    async def dry_run(
        self, proposal_id: str, user_id: str, comment: str = "",
    ) -> ActionProposal:
        """命名词(dry_run):``dispatch`` 的预演入口。

        存在的理由有二:它是路由的闭集动词之一(``getattr(service, verb)``
        要求一一对应),以及"预演"在管理台是一个**动作**,笔迹要落在审批
        轨迹上(谁按的、什么时候按的),而不只是回执。
        """
        return await self.dispatch(proposal_id, user_id, comment, dry_run=True)

    async def retry(
        self, proposal_id: str, user_id: str, comment: str = "",
    ) -> ActionProposal:
        """Re-attempt a failed dispatch (subject to ``max_attempts``)."""
        p = await self._proposal_or_raise(proposal_id)
        if p.status != "failed":
            raise ProposalError(
                f"proposal {p.id} is {p.status!r} — only a failed proposal "
                "can be retried")
        return await self.dispatch(p.id, user_id, comment)

    async def retry_due_proposals(
        self, now: datetime | None = None, *, limit: int = 50,
    ) -> int:
        """自动重试:到点的 failed 提案再试一次(退避见 ``guard.next_attempt_at``)。

        返回本次真正发起的重试次数。全部门都是**关**的默认档:

        - ``retry_backoff_base_s <= 0``(默认)→ 立即返回 0,不查库;
        - 层未启用 → 同上(自动重试是 dispatch,禁用就该安静地不动作);
        - 达到 ``max_attempts`` → 跳过(重试上限仍然只在人手里松开);
        - 上次失败**不该重试**(4xx/配置错/护栏风险拒绝,由错误文本的
          ``[ERR:...]`` 标签判定)→ 跳过 —— 同一份错配置重试一万次还是
          同一份错配置;
        - 退避未到 → 跳过;到点的判定完全从 ``(attempts, 最近一次真实投递的
          attempted_at)`` 派生,零新列,也因此**可复算**。
        """
        guards = self.guards
        if not self.enabled or int(guards.retry_backoff_base_s or 0) <= 0:
            return 0
        now_dt = now or self._now()
        candidates = await self.store.list_proposals(status="failed", limit=limit)
        retried = 0
        for p in candidates:
            if p.attempts >= self.max_attempts:
                continue
            if not classify_failure(None, p.error).retryable:
                continue
            last = _last_real_attempt(await self.store.list_deliveries(p.id))
            if last is None:
                continue
            due = next_attempt_at(
                p.attempts, last.attempted_at,
                base_s=guards.retry_backoff_base_s,
                factor=guards.retry_backoff_factor,
                max_s=guards.retry_backoff_max_s,
            )
            if due is None or due > now_dt:
                continue
            try:
                await self.retry(
                    p.id, "system",
                    f"auto-retry: backoff elapsed (attempt {p.attempts + 1})")
                retried += 1
            except ProposalError as e:
                # 预期内的控制流(护栏在等窗口、状态已变),不是故障。
                logger.info("auto-retry skipped for %s: %s", p.id, e)
            except Exception:
                logger.exception("auto-retry failed for proposal %s", p.id)
        if retried:
            logger.info("action auto-retry: %d proposal(s) re-attempted", retried)
        return retried

    # ── 预演 / 护栏 ──────────────────────────────────────

    async def _dry_run(
        self, p: ActionProposal, user_id: str, comment: str,
    ) -> ActionProposal:
        """预演:真跑前置,不发 POST,不改提案,只落轨迹。"""
        if p.status not in _DRY_RUNNABLE:
            raise ProposalError(
                f"proposal {p.id} is {p.status!r} — nothing left to dry-run "
                "(only a pending, approved or failed proposal can be previewed)")
        now = self._now().isoformat(timespec="seconds")
        channel = str((p.target or {}).get("channel") or "")
        denial = await self._guard_denial(p, channel=channel)
        body = self._envelope(p, approved_by=user_id, dispatched_at=now)
        if denial:
            ok, error, excerpt = False, denial, ""
        else:
            probe = self.dispatcher.preflight(channel, body)
            ok = probe.ok
            error = probe.error
            # 预演的产物 = "本来会发出去的那一份" —— 有界截断后存进回执行。
            excerpt = (
                json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
                [:512] if ok else "")
        await self.store.add_approval(Approval(
            proposal_id=p.id, user_id=str(user_id or ""), action="dry_run",
            comment=str(comment or ""), created_at=now,
        ))
        await self.store.add_delivery(Delivery(
            proposal_id=p.id, channel=channel, status="dry_run",
            response_excerpt=excerpt, error=error[:300], attempted_at=now,
        ))
        logger.info("action proposal %s dry-run: %s",
                    p.id, "would send" if ok else f"would fail ({error})")
        fresh = await self.store.get_proposal(p.id)
        return fresh or p

    async def _guard_denial(self, p: ActionProposal, *, channel: str) -> str:
        """护栏判定 → 拒绝原因(含 ``[ERR:...]`` 标签);放行返回空串。

        ``[ERR:...]`` 不是装饰:它让重试扫描器只凭存下来的文本就能重建
        "这次失败还该不该再试"(风险超限 = 配置问题,不重试;速率超限 =
        等窗口,重试)。
        """
        guards = self.guards
        if not risk_allowed(p.risk, guards.max_risk):
            return (f"[ERR:ACTION_CONFIG] {GUARDRAIL_PREFIX}proposal risk "
                    f"{p.risk!r} exceeds the configured ceiling "
                    f"{guards.max_risk!r}")
        if int(guards.rate_limit or 0) > 0:
            now = self._now()
            cutoff = (now - timedelta(seconds=RATE_WINDOW_S)).isoformat(
                timespec="seconds")
            times = await self.store.list_delivery_times(channel, cutoff)
            if not channel_rate_ok(
                times, now=now, limit=guards.rate_limit,
                window_s=RATE_WINDOW_S,
            ):
                return (f"[ERR:ACTION_RATE_LIMITED] {GUARDRAIL_PREFIX}channel "
                        f"{channel!r} already had {guards.rate_limit} "
                        f"attempt(s) in the last {RATE_WINDOW_S}s")
        return ""

    async def _refuse(self, p: ActionProposal, denial: str) -> None:
        """护栏拒绝的落库:提案 → failed + 原因。可审计、不静默。

        不写 approval 行(这不是人的决定)、不涨 attempts(什么都没试过)——
        两条都记在 :meth:`dispatch` 的调用方看得到的异常里。
        """
        logger.warning("action proposal %s refused by guardrail: %s", p.id, denial)
        await self.store.update_proposal(
            p.id, status="failed", error=denial[:300])

    async def ack(
        self, proposal_id: str, user_id: str, comment: str = "",
    ) -> ActionProposal:
        """Receipt: the other end has it. Idempotent — already-acked returns as-is."""
        p = await self._proposal_or_raise(proposal_id)
        if p.status not in _ACKABLE:
            raise ProposalError(
                f"proposal {p.id} is {p.status!r} — only an approved or "
                "dispatched proposal can be acked")
        if p.status == "delivered":
            return p
        now = self._now().isoformat(timespec="seconds")
        await self.store.add_delivery(Delivery(
            proposal_id=p.id, channel="ack", status="ack",
            response_excerpt=str(comment or "")[:512], attempted_at=now,
        ))
        return await self._decide(p, "delivered", user_id, "ack", comment)

    # ── housekeeping / reads ─────────────────────────────

    async def expire_due(self, now: datetime | None = None) -> int:
        """Sweep: pending proposals past their deadline → ``expired``."""
        return await self.store.expire_due(
            (now or self._now()).isoformat(timespec="seconds"))

    def _is_expired(self, p: ActionProposal) -> bool:
        if not p.expires_at:
            return False
        return p.expires_at < self._now().isoformat(timespec="seconds")

    @staticmethod
    def _envelope(
        p: ActionProposal, *, approved_by: str, dispatched_at: str,
    ) -> dict[str, Any]:
        """The rendered payload + a ``_trove`` envelope (identity for the peer).

        The envelope carries the ids a receiving system needs to dedupe on its
        side (``proposal_id`` + ``idempotency_key``) and to attribute the
        message, under one underscore-prefixed key so it cannot collide with
        the template's own fields.
        """
        body = dict(p.payload or {})
        body["_trove"] = {
            "proposal_id": p.id,
            "idempotency_key": p.idempotency_key,
            "datasource": p.datasource,
            "rule_id": p.rule_id,
            "template": p.template,
            "risk": p.risk,
            "approved_by": str(approved_by or ""),
            "dispatched_at": dispatched_at,
        }
        return body

    async def get(self, proposal_id: str) -> dict[str, Any] | None:
        """Proposal + its full audit trail (approvals, deliveries)."""
        p = await self.store.get_proposal(proposal_id)
        if p is None:
            return None
        approvals = await self.store.list_approvals(p.id)
        deliveries = await self.store.list_deliveries(p.id)
        return {
            "proposal": p,
            "approvals": approvals,
            "deliveries": deliveries,
            "stale": self._is_expired(p) and p.status in OPEN_STATUSES,
        }

    async def list_proposals(
        self, *, status: str | None = None, datasource: str | None = None,
        limit: int = 50,
    ) -> list[ActionProposal]:
        return await self.store.list_proposals(
            status=status, datasource=datasource, limit=limit)

    async def status_counts(self) -> dict[str, int]:
        return await self.store.count_by_status()
