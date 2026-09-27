"""Decision rule schema + lint.

Lint is the gate that runs *before* a rule reaches disk, so its job is to
catch the mistakes that would otherwise become a rule that quietly never
fires, or fires on a variable that does not exist at that scope.
"""

import pytest

from trove.services.decision.expr import DecisionExprError
from trove.services.decision.rules import (
    RuleError,
    compile_condition,
    lint_document,
    lint_rule,
    parse_document,
    parse_rule,
)

MINIMAL = {
    "id": "loan-drop",
    "name": "贷款余额环比下滑",
    "window": "本月",
    "subject": {"metrics": ["loan_balance"], "dimensions": ["region"]},
    "baseline": {"kind": "prev_period"},
    "scope": "per_dimension",
    "conditions": {"all": ["delta_pct < -0.10", "abs(delta) > 100000"]},
}


def rule(**overrides):
    return parse_rule({**MINIMAL, **overrides})


class TestParse:
    def test_full_rule(self):
        r = rule()
        assert r.id == "loan-drop"
        assert r.subject.metrics == ["loan_balance"]
        assert r.baseline.kind == "prev_period"
        assert r.condition_mode == "all"
        assert r.conditions == ["delta_pct < -0.10", "abs(delta) > 100000"]

    def test_conditions_accept_a_bare_list_as_and(self):
        r = rule(conditions=["delta < 0", "row_count > 0"])
        assert r.condition_mode == "all"
        assert len(r.conditions) == 2

    def test_conditions_any_mode(self):
        r = rule(conditions={"any": ["delta < 0"]})
        assert r.condition_mode == "any"

    def test_conditions_reject_mixed_all_and_any(self):
        with pytest.raises(RuleError):
            rule(conditions={"all": ["delta < 0"], "any": ["delta > 0"]})

    def test_missing_id_rejected(self):
        with pytest.raises(RuleError):
            parse_rule({"subject": {"metrics": ["m"]}})

    def test_duplicate_ids_rejected(self):
        """A job references its rule by id — a duplicate makes that ambiguous."""
        with pytest.raises(RuleError, match="duplicate rule id"):
            parse_document({"rules": [MINIMAL, {**MINIMAL, "name": "other"}]})

    def test_document_round_trip(self):
        doc = parse_document({"version": 2, "rules": [MINIMAL]})
        assert doc.version == 2
        assert [r.id for r in doc.rules] == ["loan-drop"]

    def test_empty_document(self):
        doc = parse_document(None)
        assert doc.rules == []
        assert lint_document(doc) == ["decisions.yml declares no rules"]


class TestLint:
    def test_valid_rule_is_clean(self):
        assert lint_rule(rule()) == []

    def test_metric_required(self):
        issues = lint_rule(rule(subject={"metrics": [], "dimensions": ["region"]}))
        assert any("at least one metric" in i for i in issues)

    def test_unknown_severity(self):
        assert any("severity" in i for i in lint_rule(rule(severity="loud")))

    def test_unknown_baseline_kind(self):
        assert any("baseline.kind" in i for i in lint_rule(rule(baseline={"kind": "wow"})))

    def test_literal_baseline_needs_a_value(self):
        assert any("requires a numeric value" in i
                   for i in lint_rule(rule(baseline={"kind": "literal"})))
        assert lint_rule(rule(
            baseline={"kind": "literal", "value": 1000},
            conditions=["current < baseline"],
            scope="aggregate",
        )) == []

    def test_value_without_literal_kind_is_flagged(self):
        assert any("only meaningful with kind 'literal'" in i
                   for i in lint_rule(rule(baseline={"kind": "yoy", "value": 5})))

    def test_per_dimension_needs_a_dimension(self):
        issues = lint_rule(rule(subject={"metrics": ["m"], "dimensions": []}))
        assert any("at least one subject dimension" in i for i in issues)

    def test_aggregate_rule_cannot_use_emit(self):
        issues = lint_rule(rule(scope="aggregate", emit="all"))
        assert any("emit is ignored" in i for i in issues)

    def test_top_k_only_with_emit_top_k(self):
        assert any("top_k is only meaningful" in i for i in lint_rule(rule(top_k=5)))
        assert lint_rule(rule(emit="top_k", top_k=5)) == []

    def test_dimension_only_variables_rejected_on_aggregate_rule(self):
        """`contribution` is undefined without a group-by; catching it here
        stops a rule that would silently evaluate to Unknown forever."""
        issues = lint_rule(rule(
            scope="aggregate", emit="any",
            conditions=["contribution < -0.5"],
        ))
        assert any("only exists when scope is 'per_dimension'" in i for i in issues)

    def test_baseline_variable_rejected_when_kind_is_none(self):
        issues = lint_rule(rule(baseline={"kind": "none"},
                                conditions=["current < baseline"]))
        assert any("baseline.kind is 'none'" in i for i in issues)

    def test_delta_variable_rejected_when_kind_is_none(self):
        issues = lint_rule(rule(baseline={"kind": "none"},
                                conditions=["delta_pct < -0.1"]))
        assert any("uses a delta" in i for i in issues)

    def test_bad_condition_reports_the_parse_error(self):
        issues = lint_rule(rule(conditions=["dleta < 0"]))
        assert any("dleta" in i and "bad condition" in i for i in issues)

    def test_no_conditions(self):
        assert any("at least one condition" in i
                   for i in lint_rule(rule(conditions=[])))


class TestCompileCondition:
    def test_all_mode_is_and(self):
        node = compile_condition(rule())
        assert node.eval({"delta_pct": -0.2, "delta": -200000}) is True
        assert node.eval({"delta_pct": -0.2, "delta": -1}) is False

    def test_any_mode_is_or(self):
        node = compile_condition(rule(conditions={"any": ["delta < 0", "row_count > 99"]}))
        assert node.eval({"delta": -1, "row_count": 1}) is True

    def test_single_condition_is_unwrapped(self):
        node = compile_condition(rule(conditions=["delta < 0"]))
        assert node.eval({"delta": -1}) is True

    def test_malformed_condition_raises_rather_than_returning_false(self):
        """On the execution path a bad condition is a hard failure — never
        "no trigger"."""
        with pytest.raises(DecisionExprError):
            compile_condition(rule(conditions=["dleta < 0"]))
