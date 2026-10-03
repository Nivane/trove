"""Verdict records + adjacent diffs (pure — no store, no LLM, no I/O).

The diff is the half of the decision history a human actually reads: a
verdict alone says "this is true now", two adjacent verdicts say "and here
is what changed". Every comparison must degrade to "cannot tell" on partial
evidence rather than inventing a change.
"""

from trove.services.decision.verdicts import (
    MAX_VERDICT_ROWS,
    VerdictRecord,
    diff_verdicts,
    trim_evidence,
    verdict_from_outcome,
)


class Outcome:
    """Stand-in for ``DecisionOutcome`` (duck-typed by verdict_from_outcome)."""

    def __init__(self, *, triggered=False, message="", rule_id="r1",
                 severity="warning", error="", evidence=None):
        self.triggered = triggered
        self.message = message
        self.rule_id = rule_id
        self.severity = severity
        self.error = error
        self.evidence = evidence or {}


def outcome(**overrides):
    base = {
        "triggered": True,
        "message": "贷款余额环比下滑",
        "rule_id": "loan-drop",
        "severity": "critical",
        "evidence": {
            "rule_id": "loan-drop",
            "rule_digest": "d1",
            "priority": 2,
            "times": {"anchor_date": "2026-10-01", "evaluated_at": "2026-10-01T09:00:00"},
            "rows": [{"dim": "华东", "delta_pct": -0.2, "triggered": True}],
            "evidence": {"sql_current": "SELECT 1", "rows": [[1], [2]],
                         "row_count": 2},
        },
    }
    base.update(overrides)
    return Outcome(**base)


# ── trim ─────────────────────────────────────────────────────

class TestTrimEvidence:
    def test_small_evidence_is_untouched(self):
        ev, cut = trim_evidence({"rows": [{"dim": "a"}],
                                 "evidence": {"rows": [[1]]}})
        assert cut is False
        assert ev["rows"] == [{"dim": "a"}]
        assert ev["evidence"]["rows"] == [[1]]
        assert "truncated" not in ev["evidence"]
        assert "rows_truncated" not in ev

    def test_both_row_sets_are_capped_and_flagged(self):
        ev, cut = trim_evidence({
            "rows": [{"dim": str(i)} for i in range(MAX_VERDICT_ROWS + 5)],
            "evidence": {"rows": [[i] for i in range(MAX_VERDICT_ROWS + 5)]},
        })
        assert cut is True
        assert len(ev["rows"]) == MAX_VERDICT_ROWS
        assert len(ev["evidence"]["rows"]) == MAX_VERDICT_ROWS
        assert ev["rows_truncated"] is True
        assert ev["evidence"]["truncated"] is True   # the inner flag wins inside

    def test_truncation_is_explicit_not_silent(self):
        """A cut that leaves no trace changes what a later diff can say."""
        ev, cut = trim_evidence({"rows": [{}] * (MAX_VERDICT_ROWS + 1)})
        assert cut is True and ev["rows_truncated"] is True

    def test_garbage_rows_degrade_to_empty(self):
        ev, cut = trim_evidence({"rows": "not-a-list",
                                 "evidence": {"rows": None}})
        assert cut is False
        assert ev["rows"] == []
        assert ev["evidence"]["rows"] == []

    def test_none_evidence_is_safe(self):
        ev, cut = trim_evidence(None)
        assert cut is False and ev["rows"] == []


# ── outcome → record ─────────────────────────────────────────

class TestVerdictFromOutcome:
    def test_triggered_outcome(self):
        v = verdict_from_outcome(outcome(), datasource="demo", job_id="j1",
                                 run_id=7, now="2026-10-01T09:00:05")
        assert v.status == "alert" and v.triggered is True
        assert v.rule_id == "loan-drop" and v.rule_digest == "d1"
        assert v.severity == "critical" and v.priority == 2
        assert v.row_count == 2
        assert v.anchor_date == "2026-10-01"
        assert v.evaluated_at == "2026-10-01T09:00:00"   # the service's stamp wins
        assert v.created_at == "2026-10-01T09:00:05"     # ours is the write time

    def test_untriggered_outcome(self):
        v = verdict_from_outcome(outcome(triggered=False, message=""),
                                 datasource="demo")
        assert v.status == "ok" and v.triggered is False

    def test_error_outcome_is_still_a_verdict(self):
        """"Could not judge" is a fact about the run — an absent row would
        make the history claim more than it knows."""
        v = verdict_from_outcome(outcome(error="window unresolvable",
                                         triggered=False),
                                 datasource="demo")
        assert v.status == "error" and v.error == "window unresolvable"

    def test_missing_times_falls_back_to_now(self):
        v = verdict_from_outcome(outcome(evidence={"rule_digest": "d1"}),
                                 datasource="demo", now="2026-10-02T00:00:00")
        assert v.evaluated_at == "2026-10-02T00:00:00"
        assert v.anchor_date == ""

    def test_extra_evidence_lands_under_analysis(self):
        v = verdict_from_outcome(outcome(), datasource="demo",
                                 extra_evidence={"top_components": []})
        assert v.evidence["analysis"] == {"top_components": []}

    def test_garbage_outcome_does_not_raise(self):
        v = verdict_from_outcome(object(), datasource="demo")
        assert v.status == "ok" and v.rule_id == ""


# ── adjacent diff ────────────────────────────────────────────

def rec(**overrides) -> VerdictRecord:
    base = dict(
        datasource="demo", rule_id="r1", status="alert", triggered=True,
        rule_digest="d1", evidence={
            "rows": [{"dim": "华东", "delta_pct": -0.2, "triggered": True},
                     {"dim": "华北", "delta_pct": 0.05, "triggered": False}],
        },
    )
    base.update(overrides)
    return VerdictRecord(**base)


class TestDiffVerdicts:
    def test_trigger_fired_and_cleared(self):
        fired = diff_verdicts(rec(triggered=False, status="ok"), rec())
        assert fired["trigger"] == "fired"
        assert fired["status_change"] == ["ok", "alert"]
        cleared = diff_verdicts(rec(), rec(triggered=False, status="ok"))
        assert cleared["trigger"] == "cleared"

    def test_no_trigger_move_is_none(self):
        assert diff_verdicts(rec(), rec())["trigger"] is None

    def test_rule_digest_change_is_flagged(self):
        """The one change that explains all the others: the rule was edited."""
        d = diff_verdicts(rec(), rec(rule_digest="d2"))
        assert d["rule_digest_changed"] is True
        assert d["prev_rule_digest"] == "d1" and d["rule_digest"] == "d2"

    def test_missing_digest_is_not_a_change(self):
        assert diff_verdicts(rec(rule_digest=""), rec(rule_digest="d1"))[
            "rule_digest_changed"] is False

    def test_group_fired_and_cleared(self):
        prev = rec(evidence={"rows": [
            {"dim": "华东", "delta_pct": -0.05, "triggered": False}]})
        cur = rec(evidence={"rows": [
            {"dim": "华东", "delta_pct": -0.25, "triggered": True}]})
        g = diff_verdicts(prev, cur)["groups"]
        assert g == [{"dim": "华东", "change": "fired",
                      "triggered": [False, True],
                      "delta_pct": [-0.05, -0.25]}]

    def test_group_jump_needs_the_display_threshold(self):
        prev = rec(evidence={"rows": [
            {"dim": "华东", "delta_pct": -0.10, "triggered": True}]})
        near = rec(evidence={"rows": [
            {"dim": "华东", "delta_pct": -0.15, "triggered": True}]})
        far = rec(evidence={"rows": [
            {"dim": "华东", "delta_pct": -0.30, "triggered": True}]})
        assert diff_verdicts(prev, near)["groups"] == []      # 5pp — under
        assert diff_verdicts(prev, far)["groups"][0]["change"] == "jump"

    def test_group_appeared_and_vanished(self):
        prev = rec(evidence={"rows": [{"dim": "华东", "triggered": True}]})
        cur = rec(evidence={"rows": [{"dim": "华南", "triggered": True}]})
        groups = {g["dim"]: g["change"] for g in diff_verdicts(prev, cur)["groups"]}
        assert groups == {"华东": "vanished", "华南": "appeared"}

    def test_unchanged_groups_are_omitted(self):
        assert diff_verdicts(rec(), rec())["groups"] == []

    def test_partial_evidence_degrades_to_cannot_tell(self):
        """Trimmed / legacy verdicts carry partial cards — a missing
        ``delta_pct`` must not read as a jump from zero."""
        prev = rec(evidence={"rows": [{"dim": "华东", "triggered": True}]})
        cur = rec(evidence={"rows": [
            {"dim": "华东", "delta_pct": -0.9, "triggered": True}]})
        assert diff_verdicts(prev, cur)["groups"] == []

    def test_garbled_evidence_is_safe(self):
        d = diff_verdicts(rec(evidence=None), rec(evidence={"rows": "x"}))
        assert d["groups"] == []
