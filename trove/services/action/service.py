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

**No connectors, by construction.** The constructor takes a store, a template
service, a dispatcher and config — nothing that can reach a business
datasource. The read-only posture guard test asserts this against the source.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from trove.core.logging import get_logger
from trove.services.action.dispatcher import ActionDispatcher
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

#: Where a proposal can be when ack arrives. ``approved`` counts because the
#: MCP pull channel *is* a delivery: a client that fetched an approved
#: proposal and acked it has received it, whether or not the webhook push
#: ever ran.
_ACKABLE = ("approved", "dispatched", "delivered")


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
    ) -> ActionProposal:
        """Send an approved proposal to its channel; retry a failed one."""
        p = await self._proposal_or_raise(proposal_id)
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
        prior = await self.store.list_deliveries(p.id)
        is_retry = p.status == "failed" or bool(prior)
        now = self._now().isoformat(timespec="seconds")
        channel = str((p.target or {}).get("channel") or "")
        body = self._envelope(p, approved_by=user_id, dispatched_at=now)
        result = await self.dispatcher.send(channel, body)

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
            logger.warning("action proposal %s dispatch failed: %s",
                           p.id, result.error)
            await self._decide(
                p, "failed", user_id, "retry" if is_retry else "dispatch",
                comment, attempts=attempts, error=result.error[:300])
        fresh = await self.store.get_proposal(p.id)
        return fresh or p

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
