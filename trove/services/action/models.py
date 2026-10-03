"""Action-pillar records — templates, proposals, approvals, deliveries.

The action pillar is the last leg of 「问数 · 分析 · 决策 · 行动」: a decision
verdict that fired is turned into a **proposal**, a human approves it, and only
then does anything leave the system. The records here are the nouns of that
flow.

**Structural boundary (read-only posture).** Nothing in this package writes to
a business datasource, and the way that stays true is structural rather than
conventional: ``ActionService`` / ``ActionDispatcher`` take no connector
registry and this package never imports ``services.datasource`` or the SQL
guard. ``execute_unsafe`` keeps exactly zero callers. The guard test
(``tests/services/action/test_readonly_posture.py``) asserts it against the
source, so a future edit that reaches for a connector fails loudly at test
time instead of quietly at review time.

**One proposal is one outbound message.** The rendered payload is stored on
the proposal at creation time and never re-rendered: what a human approved is
byte-for-byte what gets sent. Re-rendering at dispatch time would let an
edited rule or template change a message that was already approved — the exact
divergence the approval step exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Proposal lifecycle. ``delivered`` is reached through ``ack`` (a human or an
#: MCP client confirming the other end has it); ``dispatched`` means the
#: channel accepted it but nobody has confirmed receipt yet.
PROPOSAL_STATUSES = (
    "pending", "approved", "rejected", "expired", "cancelled",
    "dispatched", "delivered", "failed",
)

#: Statuses a proposal can still move out of. Everything else is history.
OPEN_STATUSES = ("pending", "approved", "failed")

#: ``action_type`` — v1 closed set. Both go out through a named channel; the
#: difference is presentation (``notify`` = alert-shaped, ``webhook`` = the
#: template's payload verbatim).
ACTION_TYPES = ("notify", "webhook")

#: ``risk`` — shown to the approver. Same three levels the rest of the console
#: uses; free-form risk labels would not be comparable across templates.
RISKS = ("low", "medium", "high")

#: Template gate statuses (draft → admin confirm), mirroring SkillService.
TEMPLATE_STATUSES = ("pending", "confirmed")

#: What an approval row can record. ``retry`` is recorded when a failed
#: dispatch is re-attempted — the audit line answers "who sent this twice".
APPROVAL_ACTIONS = ("approve", "reject", "cancel", "dispatch", "retry", "ack")

#: v1 is single-approver only. The field exists on the template so the
#: multi-approver version does not have to migrate files, but any other value
#: is refused loudly at create/read time (a "2" that silently behaves like "1"
#: would be the worst of both).
APPROVALS_REQUIRED_V1 = 1


@dataclass
class ActionTemplate:
    """An org response asset: what to send, over which channel, at what risk.

    ``payload_template`` is a JSON document with ``{{variable}}`` placeholders
    from a closed set (see ``template_render``) — deliberately not a template
    *language*: there is no Jinja, no expressions, no eval. A template is data
    an admin confirms, and the variables it may interpolate are exactly the
    ones the decision layer can supply.
    """

    name: str
    title: str = ""
    description: str = ""
    status: str = "pending"          # TEMPLATE_STATUSES
    action_type: str = "notify"      # ACTION_TYPES
    target: dict[str, Any] = field(default_factory=dict)   # {channel, resource}
    risk: str = "low"                # RISKS
    approvals_required: int = APPROVALS_REQUIRED_V1
    payload_template: str = ""
    source: str = "admin"
    created_at: str = ""
    updated_at: str = ""
    #: sha256 of the template file, first 16 hex chars — recorded on every
    #: proposal so "which version of the template did we send" survives edits.
    digest: str = ""


@dataclass
class ActionProposal:
    """One outbound action awaiting (or past) human approval.

    ``origin_kind`` records where it came from: ``verdict`` (a decision rule
    fired — the automated path) or ``manual`` (an admin composed it). The
    automated path is the only one wired in v1; the field exists because the
    audit question "why did this leave the building" has different answers.
    """

    id: str
    datasource: str = ""
    rule_id: str = ""
    rule_digest: str = ""
    run_id: int | None = None
    job_id: str = ""
    origin_kind: str = "verdict"     # verdict | manual
    action_type: str = "notify"
    template: str = ""
    template_digest: str = ""
    target: dict[str, Any] = field(default_factory=dict)
    #: The rendered payload, frozen at creation — see the module docstring.
    payload: dict[str, Any] = field(default_factory=dict)
    #: The rule's human-authored recommendation at fire time (why this exists).
    rationale: str = ""
    #: Pointers back into the evidence: verdict id / run id / job id.
    evidence_refs: dict[str, Any] = field(default_factory=dict)
    severity: str = ""
    priority: int = 0
    risk: str = "low"
    status: str = "pending"          # PROPOSAL_STATUSES
    idempotency_key: str = ""
    created_by: str = "system"
    created_at: str = ""
    decided_at: str = ""
    expires_at: str = ""
    dispatched_at: str = ""
    attempts: int = 0
    error: str = ""


@dataclass
class Approval:
    """One human decision on a proposal (append-only audit row)."""

    proposal_id: str
    user_id: str = ""
    action: str = "approve"          # APPROVAL_ACTIONS
    comment: str = ""
    created_at: str = ""
    id: int | None = None


@dataclass
class Delivery:
    """One outbound attempt (or ack) — the receipt trail.

    A dispatch is not "done" because a request was made: the delivery row
    carries the channel, the HTTP status and a bounded response excerpt, and
    ``ack`` writes a row too (channel ``ack``) so the receipt is in the same
    trail rather than a flag somewhere else.
    """

    proposal_id: str
    channel: str = ""
    status: str = "sent"             # sent | failed | ack
    http_status: int | None = None
    response_excerpt: str = ""
    error: str = ""
    attempted_at: str = ""
    id: int | None = None


def is_open(status: str) -> bool:
    return status in OPEN_STATUSES
