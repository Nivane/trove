"""Proposal construction — pure functions, no I/O, no LLM.

Everything here turns a fired verdict + a confirmed template into an
``ActionProposal``. Keeping it pure is what makes the interesting questions
testable without a database: *which* group's numbers a payload names, what the
idempotency key is made of, and which variables a template may interpolate.

**Idempotency.** A decision job that re-runs (a manual re-run, a schedule that
advanced twice, a retry after a crash) must not produce a second proposal for
the same firing. The key is
``sha256(rule_id | rule_digest | anchor_date | template | params)`` — the same
firing re-judged from the same rule version on the same anchor day collapses
to one proposal, while a rule edited between runs (new digest) or a new day
(new anchor) legitimately produces a new one. Deliberately *not* keyed on the
numbers: a re-run whose data changed slightly is still the same firing, and
keying on values would spam a proposal per tick.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from trove.services.action.models import ActionProposal, ActionTemplate
from trove.services.action.template_render import (
    TEMPLATE_VARIABLES,
    render_payload,
)


class ProposalError(ValueError):
    """A proposal that must not be created (bad template, bad params)."""


def idempotency_key(
    rule_id: str, rule_digest: str, anchor_date: str, template: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Stable key for "the same firing, same rule version, same day"."""
    parts = [
        str(rule_id or ""),
        str(rule_digest or ""),
        str(anchor_date or ""),
        str(template or ""),
        json.dumps(params or {}, sort_keys=True, ensure_ascii=False, default=str),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _fmt(value: Any) -> Any:
    """Card value → payload value; absent stays empty (never ``"None"``)."""
    if value is None:
        return ""
    return value


def anchor_of(outcome: Any, now: datetime | None = None) -> str:
    """The verdict's anchor date, falling back to the wall clock's date."""
    times = (getattr(outcome, "evidence", None) or {}).get("times")
    if isinstance(times, dict) and times.get("anchor_date"):
        return str(times["anchor_date"])
    return (now or datetime.now()).date().isoformat()


def proposal_key(
    rule: Any, outcome: Any, template_name: str,
    now: datetime | None = None,
) -> str:
    """The dedup key for this (rule version, firing, template).

    Extracted so the service can check for a duplicate *before* rendering the
    payload — same inputs as ``build_proposal`` uses, one implementation.
    """
    rule_digest = str(
        (getattr(outcome, "evidence", None) or {}).get("rule_digest") or "")
    params = dict(getattr(getattr(rule, "action", None), "params", None) or {})
    return idempotency_key(
        getattr(rule, "id", ""), rule_digest, anchor_of(outcome, now),
        template_name, params)


def primary_group(outcome: Any) -> dict[str, Any]:
    """The fired group whose numbers the payload names.

    A ``scope: per_dimension`` rule can fire for several groups at once (and
    ``emit: top_k`` reports the top ones), but an outbound message names one:
    the group that moved the most, by the same ``|contribution|`` ordering the
    decision engine uses to rank. An aggregate rule has exactly one row
    (``dim == ""``) and this returns it unchanged.
    """
    evidence = getattr(outcome, "evidence", None) or {}
    rows = evidence.get("rows")
    if not isinstance(rows, list):
        return {}
    fired = [r for r in rows if isinstance(r, dict) and r.get("triggered")]
    if not fired:
        return {}
    fired.sort(key=lambda r: abs(r.get("contribution") or 0.0), reverse=True)
    return fired[0]


def build_variables(
    rule: Any, outcome: Any, datasource: str, *,
    proposal_id: str, run_id: int | None = None, job_id: str = "",
) -> dict[str, Any]:
    """The closed-set variable values this rule + verdict can supply.

    Every key of :data:`TEMPLATE_VARIABLES` is present, so a template that
    references any legal variable renders — with the *empty* string where the
    rule genuinely has no such value (``dim`` on an aggregate rule, the deltas
    when ``baseline.kind`` is ``none``). Empty-as-absent is faithful here, not
    silent: the decision engine uses the same ``""`` group label for aggregate
    judgments. A template naming something *outside* the closed set is still a
    hard error (``template_render``).

    ``action.params`` are the rule author's literal overrides and win over the
    derived values — that is the whole point of the field (pin a channel-side
    label, restate the message in the org's words). Unknown keys are refused:
    a param that can never reach a payload is a typo, not a feature.
    """
    evidence = getattr(outcome, "evidence", None) or {}
    times = evidence.get("times") if isinstance(evidence.get("times"), dict) else {}
    card = primary_group(outcome)
    metrics = list(getattr(getattr(rule, "subject", None), "metrics", []) or [])

    variables: dict[str, Any] = {
        "rule_id": getattr(rule, "id", "") or "",
        "rule_name": rule.describe() if hasattr(rule, "describe") else "",
        "rule_digest": str(evidence.get("rule_digest") or ""),
        "severity": str(getattr(rule, "severity", "") or ""),
        "priority": int(getattr(rule, "priority", 0) or 0),
        "message": str(getattr(outcome, "message", "") or ""),
        "recommendation": str(getattr(rule, "recommendation", "") or ""),
        "datasource": str(datasource or ""),
        "metric": str(metrics[0]) if metrics else "",
        "dim": str(card.get("dim") or ""),
        "current": _fmt(card.get("current")),
        "baseline": _fmt(card.get("baseline")),
        "delta": _fmt(card.get("delta")),
        "delta_pct": _fmt(card.get("delta_pct")),
        "anchor_date": str(times.get("anchor_date") or ""),
        "evaluated_at": str(times.get("evaluated_at") or ""),
        "proposal_id": proposal_id,
        "run_id": run_id if run_id is not None else "",
        "job_id": str(job_id or ""),
    }

    action = getattr(rule, "action", None)
    params = dict(getattr(action, "params", None) or {})
    unknown = sorted(set(params) - TEMPLATE_VARIABLES)
    if unknown:
        raise ProposalError(
            f"rule {getattr(rule, 'id', '?')!r}: action.params names "
            f"{', '.join(repr(u) for u in unknown)}, which "
            f"{'is' if len(unknown) == 1 else 'are'} not template "
            f"variable(s) — allowed: {', '.join(sorted(TEMPLATE_VARIABLES))}")
    variables.update(params)
    return variables


def build_proposal(
    *, rule: Any, outcome: Any, datasource: str, template: ActionTemplate,
    run_id: int | None = None, job_id: str = "", created_by: str = "system",
    ttl_hours: int = 72, max_payload_bytes: int = 0,
    evidence_refs: dict[str, Any] | None = None,
    now: datetime | None = None, proposal_id: str = "",
) -> ActionProposal:
    """A fired outcome + a confirmed template → a pending ``ActionProposal``.

    Raises ``ProposalError``/``RenderError`` when the payload cannot be built —
    the caller creates *no* proposal in that case (a half-rendered outbound
    message is worse than a missing one), and the failure is loud.
    """
    now = now or datetime.now()
    created_at = now.isoformat(timespec="seconds")
    pid = proposal_id or ("p-" + uuid.uuid4().hex[:16])
    rule_digest = str(
        (getattr(outcome, "evidence", None) or {}).get("rule_digest") or "")

    variables = build_variables(
        rule, outcome, datasource, proposal_id=pid, run_id=run_id, job_id=job_id)
    payload = render_payload(
        template.payload_template, variables, max_bytes=max_payload_bytes)

    return ActionProposal(
        id=pid,
        datasource=str(datasource or ""),
        rule_id=str(getattr(rule, "id", "") or ""),
        rule_digest=rule_digest,
        run_id=run_id,
        job_id=str(job_id or ""),
        origin_kind="verdict",
        action_type=template.action_type,
        template=template.name,
        template_digest=template.digest,
        target=dict(template.target or {}),
        payload=payload,
        rationale=str(getattr(rule, "recommendation", "") or ""),
        evidence_refs=dict(evidence_refs or {}),
        severity=str(getattr(rule, "severity", "") or ""),
        priority=int(getattr(rule, "priority", 0) or 0),
        risk=template.risk,
        status="pending",
        idempotency_key=proposal_key(rule, outcome, template.name, now),
        created_by=str(created_by or "system"),
        created_at=created_at,
        expires_at=(now + timedelta(hours=max(1, int(ttl_hours)))
                    ).isoformat(timespec="seconds"),
    )
