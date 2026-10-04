"""Verdict records + adjacent diffs — the audit trail of the decision layer.

A decision run leaves two artifacts: the run row (jobs DB) and a **verdict**
(here). The run row answers "did the schedule execute"; the verdict answers
"what was judged, on which numbers, under which version of the rule" — and,
because decision rules fire on *change*, the single most useful thing to show
next to a verdict is the previous one and what moved between them.

Three deliberate properties:

**Records are immutable.** There is no edit and no rollback. A verdict is a
statement about a moment ("on 2026-10-01 the aggregate said X"); editing it
would be editing history, and the whole point of storing the evidence is that
later disagreement can be argued against the *record*, not against a mutable
row. The diff is a derived view over two records, never stored.

**The diff is pure and works on absent fields.** Old verdicts (or ones
trimmed by the row cap) carry partial evidence; every comparison degrades to
"cannot tell" rather than inventing a change. ``rule_digest_changed`` is
called out separately because it is the one change that explains all the
others: the rule itself was edited between the two runs.

**No LLM anywhere.** Same discipline as ``service.py`` / ``rules.py`` — a
verdict that depends on a model is not a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trove.services.decision.expr import as_number

#: Evidence rows kept *in the verdict store*. The run record (jobs DB) keeps
#: ``MAX_EVIDENCE_ROWS = 200``; the verdict is the long-lived audit row, so it
#: keeps a tighter sample plus an explicit ``evidence_truncated`` flag.
MAX_VERDICT_ROWS = 50

#: Group-level "magnitude jump": |Δpct_now − Δpct_prev| ≥ 10 percentage points
#: counts as a change worth highlighting. Not a statistical claim — a display
#: threshold, stated here so the UI and the tests share one number.
MAGNITUDE_JUMP = 0.10


@dataclass
class VerdictRecord:
    """One decision evaluation, as stored.

    ``status`` mirrors the run row (``ok`` / ``alert`` / ``error``); a verdict
    with ``error`` set is still a verdict — "could not judge" is a fact about
    the run that must be visible in the history, not an absent row.
    """

    datasource: str
    rule_id: str
    status: str = "ok"
    triggered: bool = False
    rule_digest: str = ""
    run_id: int | None = None
    job_id: str = ""
    severity: str = ""
    priority: int = 0
    message: str = ""
    error: str = ""
    row_count: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)
    evidence_truncated: bool = False
    anchor_date: str = ""
    evaluated_at: str = ""
    created_at: str = ""
    id: int | None = None


def _trim_rows(rows: Any, limit: int) -> tuple[list[Any], bool]:
    if not isinstance(rows, list):
        return [], False
    return rows[:limit], len(rows) > limit


def trim_evidence(
    evidence: dict[str, Any], *, max_rows: int = MAX_VERDICT_ROWS,
) -> tuple[dict[str, Any], bool]:
    """Bound a verdict's evidence for storage → ``(evidence, truncated)``.

    Both row sets are trimmed: the raw result sample (``evidence.evidence
    .rows``) and the per-group cards (``evidence.rows``). The cards are what
    the adjacent diff reads, so cutting them changes what a later diff can
    say — which is exactly why the flag is stored rather than the cut being
    silent.
    """
    out = dict(evidence or {})
    truncated = False
    inner = out.get("evidence")
    if isinstance(inner, dict):
        inner = dict(inner)
        kept, cut = _trim_rows(inner.get("rows"), max_rows)
        inner["rows"] = kept
        if cut:
            inner["truncated"] = True
        out["evidence"] = inner
        truncated = truncated or cut
    cards, cut = _trim_rows(out.get("rows"), max_rows)
    out["rows"] = cards
    if cut:
        out["rows_truncated"] = True
    truncated = truncated or cut
    return out, truncated


def verdict_from_outcome(
    outcome: Any, *, datasource: str, job_id: str = "",
    run_id: int | None = None, now: str = "",
    extra_evidence: dict[str, Any] | None = None,
) -> VerdictRecord:
    """``DecisionOutcome`` + run identity → ``VerdictRecord`` (pure).

    ``extra_evidence`` (the analysis bridge's summary) is merged in by the
    caller that owns it — the verdict module does not know about the bridge,
    and the store must not grow a schema for it: the evidence JSON carries it
    verbatim.
    """
    evidence = dict(getattr(outcome, "evidence", None) or {})
    if extra_evidence:
        evidence["analysis"] = extra_evidence
    evidence, truncated = trim_evidence(evidence)
    times = evidence.get("times") if isinstance(evidence.get("times"), dict) else {}
    triggered = bool(getattr(outcome, "triggered", False))
    error = str(getattr(outcome, "error", "") or "")
    status = "error" if error else ("alert" if triggered else "ok")
    return VerdictRecord(
        datasource=datasource,
        rule_id=str(getattr(outcome, "rule_id", "") or ""),
        status=status,
        triggered=triggered,
        rule_digest=str(evidence.get("rule_digest", "") or ""),
        run_id=run_id,
        job_id=job_id,
        severity=str(getattr(outcome, "severity", "") or ""),
        priority=int(evidence.get("priority", 0) or 0),
        message=str(getattr(outcome, "message", "") or ""),
        error=error,
        row_count=int((evidence.get("evidence") or {}).get("row_count") or 0),
        evidence=evidence,
        evidence_truncated=truncated,
        anchor_date=str(times.get("anchor_date", "") or ""),
        evaluated_at=str(times.get("evaluated_at", "") or now),
        created_at=now,
    )


# ── adjacent diff (pure) ─────────────────────────────────────

def _cards(rec: VerdictRecord) -> dict[str, dict[str, Any]]:
    """Per-group cards keyed by dim label; missing/garbled → {}."""
    rows = (rec.evidence or {}).get("rows")
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if isinstance(r, dict) and r.get("dim") is not None:
            out[str(r.get("dim"))] = r
    return out


def _delta_pct(card: dict[str, Any]) -> float | None:
    return as_number(card.get("delta_pct"))


def _rev_of(rec: VerdictRecord) -> str:
    """该 verdict 记下的 ``rule_rev``(B2 之前的行没有 → 空串)。

    与 ``rule_digest`` 的区别就是 N2:digest 是整份 decisions.yml 的
    字节 hash —— 编辑 B 规则会让 A 规则的相邻两条也"digest 变了";
    rev 是单条规则的内容版本,只在该规则真的被改过时才变。
    """
    evidence = rec.evidence if isinstance(rec.evidence, dict) else {}
    return str(evidence.get("rule_rev") or "")


def diff_verdicts(prev: VerdictRecord, cur: VerdictRecord) -> dict[str, Any]:
    """Two adjacent verdicts → what changed between them (pure, no LLM).

    "Adjacent" means same rule, consecutive evaluations — the caller owns
    that ordering (``list_for_rule`` returns newest-first). Every field is
    present even when empty, so the UI never has to distinguish "no change"
    from "key missing".

    ``rule_digest_changed`` 与 ``rule_rev_changed`` 是两把尺:前者对整份
    decisions.yml 敏感(编辑别的规则也亮),后者只对这条规则敏感 ——
    渲染要说"规则被改过"时,信 rev(见 :func:`_rev_of`)。
    """
    digest_changed = bool(prev.rule_digest) and bool(cur.rule_digest) \
        and prev.rule_digest != cur.rule_digest
    trigger = None
    if cur.triggered and not prev.triggered:
        trigger = "fired"
    elif prev.triggered and not cur.triggered:
        trigger = "cleared"

    groups: list[dict[str, Any]] = []
    prev_cards, cur_cards = _cards(prev), _cards(cur)
    for dim in sorted(set(prev_cards) | set(cur_cards)):
        a, b = prev_cards.get(dim), cur_cards.get(dim)
        if a is None:
            groups.append({"dim": dim, "change": "appeared",
                           "triggered": [None, bool(b.get("triggered"))]})
            continue
        if b is None:
            groups.append({"dim": dim, "change": "vanished",
                           "triggered": [bool(a.get("triggered")), None]})
            continue
        change = None
        if bool(b.get("triggered")) and not bool(a.get("triggered")):
            change = "fired"
        elif bool(a.get("triggered")) and not bool(b.get("triggered")):
            change = "cleared"
        else:
            before, after = _delta_pct(a), _delta_pct(b)
            if before is not None and after is not None \
                    and abs(after - before) >= MAGNITUDE_JUMP:
                change = "jump"
        if change is not None:
            groups.append({
                "dim": dim, "change": change,
                "triggered": [bool(a.get("triggered")), bool(b.get("triggered"))],
                "delta_pct": [_delta_pct(a), _delta_pct(b)],
            })

    status_change = [prev.status, cur.status] if prev.status != cur.status else None
    # rev 变更与 digest 变更同款三态:两边都有值才敢说"变了" —— 缺 rev 的
    # 是 B2 之前的行,"没记录版本"不是"版本变了"(同 digest 的纪律)。
    prev_rev, cur_rev = _rev_of(prev), _rev_of(cur)
    return {
        "prev_id": prev.id,
        "rule_digest_changed": digest_changed,
        "prev_rule_digest": prev.rule_digest,
        "rule_digest": cur.rule_digest,
        "rule_rev_changed": bool(prev_rev) and bool(cur_rev)
        and prev_rev != cur_rev,
        "prev_rule_rev": prev_rev,
        "rule_rev": cur_rev,
        "status_change": status_change,
        "trigger": trigger,
        "groups": groups,
    }
