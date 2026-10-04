"""Decision rule schema + lint.

Lint is the gate that runs *before* a rule reaches disk, so its job is to
catch the mistakes that would otherwise become a rule that quietly never
fires, or fires on a variable that does not exist at that scope.
"""

import pytest

from trove.services.decision.expr import DecisionExprError
from trove.services.decision.rules import (
    PRIORITY_MAX,
    SCHEMA_VERSION,
    ActionRef,
    RuleError,
    Seasonal,
    Significance,
    compile_condition,
    lint_advisories,
    lint_document,
    lint_document_assets,
    lint_rule,
    lint_rule_assets,
    parse_document,
    parse_rule,
    rule_rev,
    rule_to_dict,
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

#: A schema-v2 rule exercising every new field (round-trip + lint source).
V2 = {
    **MINIMAL,
    "recommendation": "联系区域客户经理复核贷款余额变动",
    "priority": 2,
    "driver_dimension": "product",
    "action": {
        "template": "notify-ops",
        "autonomy": "propose",
        "params": {"channel": "ops-alerts"},
    },
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
        """Empty is not an issue: it is what a placeholder file looks like,
        and a whole-document write is the only way to remove the last rule —
        so flagging it would make that rule undeletable."""
        doc = parse_document(None)
        assert doc.rules == []
        assert lint_document(doc) == []

    def test_newer_document_version_refused(self):
        """Same philosophy as ``StorageSchemaTooNew``: reading less than the
        file holds is silent, and the next save would make it permanent."""
        with pytest.raises(RuleError, match="newer than this Trove understands"):
            parse_document({"version": SCHEMA_VERSION + 1, "rules": []})

    def test_current_version_is_readable(self):
        doc = parse_document({"version": SCHEMA_VERSION, "rules": [V2]})
        assert doc.version == SCHEMA_VERSION
        assert doc.rules[0].action.template == "notify-ops"


class TestLint:
    def test_valid_rule_is_clean(self):
        assert lint_rule(rule()) == []

    def test_metric_required(self):
        issues = lint_rule(rule(subject={"metrics": [], "dimensions": ["region"]}))
        assert any("must name a metric" in i for i in issues)

    def test_exactly_one_metric(self):
        """`current`/`delta` are singular — a second metric has nowhere to
        bind, so reject rather than silently judging only the first."""
        issues = lint_rule(rule(subject={"metrics": ["a", "b"],
                                         "dimensions": ["region"]}))
        assert any("exactly one metric" in i for i in issues)

    def test_unknown_severity(self):
        assert any("severity" in i for i in lint_rule(rule(severity="loud")))

    def test_unknown_baseline_kind(self):
        assert any("baseline.kind" in i for i in lint_rule(rule(baseline={"kind": "wow"})))

    def test_literal_baseline_needs_a_value(self):
        assert any("requires a numeric value" in i
                   for i in lint_rule(rule(baseline={"kind": "literal"})))
        assert lint_rule(rule(
            baseline={"kind": "literal", "value": 1000},
            subject={"metrics": ["m"]},
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
        issues = lint_rule(rule(scope="aggregate", emit="all",
                                subject={"metrics": ["m"]}))
        assert any("emit is ignored" in i for i in issues)

    def test_aggregate_rule_cannot_declare_dimensions(self):
        """The dimensions *are* the group-by, so the query returns one row per
        group while the rule looks up a single aggregate row — every variable
        reads Unknown and the rule can never fire. Blocking it at lint time is
        the difference between a rejected rule and a silently dead one."""
        issues = lint_rule(rule(scope="aggregate", emit="any"))
        assert any("subject.dimensions" in i for i in issues)

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


class TestSchemaV2Parse:
    def test_defaults_read_as_v1(self):
        """A rule written before v2 means exactly what it always meant."""
        r = rule()
        assert r.recommendation == ""
        assert r.priority == 0
        assert r.action is None
        assert r.driver_dimension == ""

    def test_full_v2_rule(self):
        r = parse_rule(V2)
        assert r.recommendation == V2["recommendation"]
        assert r.priority == 2
        assert r.driver_dimension == "product"
        assert r.action == ActionRef(
            template="notify-ops", autonomy="propose",
            params={"channel": "ops-alerts"})

    def test_action_defaults_to_notify_only(self):
        r = rule(action={"template": "notify-ops"})
        assert r.action.autonomy == "notify_only"
        assert r.action.params == {}

    def test_priority_garbage_raises(self):
        """``priority: high`` must not quietly become 0 — 'regular priority'
        reads exactly like a deliberate choice."""
        with pytest.raises(RuleError):
            rule(priority="high")
        with pytest.raises(RuleError):
            rule(priority=True)   # bool is not an int here

    def test_priority_absent_or_empty_means_zero(self):
        assert rule(priority=None).priority == 0
        assert rule(priority="").priority == 0

    def test_action_must_be_a_mapping(self):
        with pytest.raises(RuleError):
            rule(action="notify-ops")

    def test_round_trip_is_value_equal(self):
        """The API writes back what it read: a field ``parse_rule`` reads but
        ``rule_to_dict`` drops would vanish on the next save through the UI."""
        original = parse_rule(V2)
        assert parse_rule(rule_to_dict(original)) == original

    def test_round_trip_of_a_bare_rule(self):
        original = rule()
        assert parse_rule(rule_to_dict(original)) == original

    def test_round_trip_keeps_action_params(self):
        original = rule(action={"template": "t", "autonomy": "propose",
                                "params": {"channel": "ops", "n": 3}})
        again = parse_rule(rule_to_dict(original))
        assert again.action.params == {"channel": "ops", "n": 3}


class TestLintV2:
    def test_priority_range_blocks(self):
        assert any("priority must be an integer in [0, 3]" in i
                   for i in lint_rule(rule(priority=PRIORITY_MAX + 1)))
        assert any("priority must be an integer" in i
                   for i in lint_rule(rule(priority=-1)))
        assert lint_rule(rule(priority=PRIORITY_MAX)) == []

    def test_empty_action_template_blocks(self):
        assert any("action.template must not be empty" in i
                   for i in lint_rule(rule(action={"autonomy": "propose"})))

    def test_unknown_autonomy_blocks(self):
        issues = lint_rule(rule(action={"template": "t", "autonomy": "auto"}))
        assert any("action.autonomy must be one of notify_only, propose" in i
                   for i in issues)

    def test_a_v2_rule_is_still_clean(self):
        """The new fields must not make a well-formed rule report issues."""
        assert lint_rule(parse_rule(V2)) == []


class TestAdvisories:
    """Advisories never block a save — they are the admin UI's nudge."""

    def test_action_without_recommendation(self):
        doc = parse_document({"rules": [
            {**MINIMAL, "action": {"template": "notify-ops"}},
        ]})
        assert lint_document(doc) == []      # not blocking
        assert any("no 'recommendation'" in i for i in lint_advisories(doc))

    def test_propose_on_aggregate_suggests_driver_dimension(self):
        doc = parse_document({"rules": [{
            "id": "agg", "subject": {"metrics": ["m"]},
            "baseline": {"kind": "prev_period"},
            "conditions": ["delta < 0"],
            "recommendation": "去看一眼",
            "action": {"template": "t", "autonomy": "propose"},
        }]})
        assert any("driver_dimension" in i for i in lint_advisories(doc))

    def test_clean_rule_has_no_advisories(self):
        assert lint_advisories(parse_document({"rules": [V2]})) == []


class TestLintAssets:
    """``None`` = registry unavailable (skip); an empty set = present and
    empty (flag). The difference is what keeps P3 wiring honest: an unwired
    service must not fail rules, an empty one must."""

    def test_unavailable_registries_skip(self):
        r = rule(action={"template": "ghost"}, driver_dimension="product")
        assert lint_rule_assets(r) == []

    def test_declared_missing_from_registry(self):
        r = rule(action={"template": "ghost"})
        issues = lint_rule_assets(r, templates=set(), confirmed=set())
        assert any("'ghost' is not declared" in i for i in issues)

    def test_declared_but_unconfirmed(self):
        r = rule(action={"template": "notify-ops"})
        issues = lint_rule_assets(r, templates={"notify-ops"}, confirmed=set())
        assert any("not confirmed yet" in i for i in issues)
        assert lint_rule_assets(
            r, templates={"notify-ops"}, confirmed={"notify-ops"}) == []

    def test_driver_dimension_must_be_declared(self):
        r = rule(driver_dimension="product")
        issues = lint_rule_assets(r, dimensions={"region"})
        assert any("driver_dimension 'product' is not declared" in i
                   for i in issues)
        assert lint_rule_assets(r, dimensions={"product"}) == []

    def test_document_assets_flatten(self):
        doc = parse_document({"rules": [V2, {**MINIMAL, "id": "other"}]})
        issues = lint_document_assets(doc, templates={"a"}, confirmed={"a"})
        assert len(issues) == 1
        assert "notify-ops" in issues[0]


#: A schema-v3 rule exercising the significance/seasonal declarations.
V3 = {
    **MINIMAL,
    "seasonal": {"grain": "month", "lookback": 12, "mode": "same_phase", "k": 4},
    "significance": {"require": "outside_band", "min_confidence": 0.2},
}


class TestSchemaV3Parse:
    def test_defaults_read_as_v2(self):
        """A rule written before v3 means exactly what it always meant:
        the declarations are None, not default-constructed."""
        r = rule()
        assert r.seasonal is None
        assert r.significance is None

    def test_full_v3_rule(self):
        r = parse_rule(V3)
        assert r.seasonal == Seasonal(grain="month", lookback=12,
                                      mode="same_phase", k=4.0)
        assert r.significance == Significance(require="outside_band",
                                              min_confidence=0.2)
        assert r.significance.required() is True

    def test_seasonal_defaults(self):
        r = rule(seasonal={})
        assert r.seasonal == Seasonal(grain="", lookback=12,
                                      mode="trailing", k=3.5)

    def test_presence_is_the_declaration(self):
        """显式写出与缺省相同的块也是声明 —— 存在性必须能表达,不能靠
        「值不等于缺省」来猜。"""
        assert parse_rule({**MINIMAL, "seasonal": {}}).seasonal is not None
        assert rule(seasonal=None).seasonal is None
        assert rule(seasonal="").seasonal is None

    def test_significance_defaults_to_record_only(self):
        r = rule(significance={})
        assert r.significance is not None
        assert r.significance.required() is False
        assert r.significance.min_confidence == 0.0

    def test_garbage_numbers_raise(self):
        with pytest.raises(RuleError):
            rule(seasonal={"k": "loose"})
        with pytest.raises(RuleError):
            rule(seasonal={"lookback": True})
        with pytest.raises(RuleError):
            rule(significance={"min_confidence": "high"})
        with pytest.raises(RuleError):
            rule(significance="yes")

    def test_round_trip_is_value_equal(self):
        original = parse_rule(V3)
        assert parse_rule(rule_to_dict(original)) == original

    def test_round_trip_of_a_defaulted_v3_rule(self):
        original = rule(seasonal={}, significance={})
        assert parse_rule(rule_to_dict(original)) == original

    def test_v2_rule_serializes_byte_identically(self):
        """条件序列化:未声明 v3 字段的规则,写回形状与历史完全一致 ——
        digest 与 rule_rev 的连续性(audit bucket)以此为前提。"""
        out = rule_to_dict(parse_rule(V2))
        assert "seasonal" not in out and "significance" not in out


class TestLintV3:
    def test_a_v3_rule_is_clean(self):
        assert lint_rule(parse_rule(V3)) == []

    def test_significance_requires_seasonal(self):
        issues = lint_rule(rule(significance={"require": "outside_band"}))
        assert any("requires a 'seasonal' block" in i for i in issues)

    def test_unknown_require_and_grain_and_mode(self):
        assert any("significance.require must be one of" in i for i in
                   lint_rule(rule(seasonal={}, significance={"require": "maybe"})))
        assert any("seasonal.grain must be one of" in i for i in
                   lint_rule(rule(seasonal={"grain": "quarter"})))
        assert any("seasonal.mode must be one of" in i for i in
                   lint_rule(rule(seasonal={"mode": "yoy"})))

    def test_lookback_and_k_bounds(self):
        assert any("seasonal.lookback must be >= 1" in i for i in
                   lint_rule(rule(seasonal={"lookback": 0})))
        assert any("seasonal.k must be > 0" in i for i in
                   lint_rule(rule(seasonal={"k": 0})))

    def test_min_confidence_range(self):
        issues = lint_rule(rule(seasonal={},
                                significance={"min_confidence": 1.5}))
        assert any("min_confidence must be in [0, 1]" in i for i in issues)

    def test_requiring_with_too_few_blocks_blocks_the_write(self):
        """MIN_BLOCKS 是统计侧的硬门:声明了门却给不够块,等于一条每次
        都 error 的规则 —— 挡在写入口,而不是等它在生产里天天报错。"""
        issues = lint_rule(rule(seasonal={"lookback": 4},
                                significance={"require": "outside_band"}))
        assert any("MIN_BLOCKS" in i for i in issues)
        # 只记录不拦的门不受此限(降级记账即可)
        assert lint_rule(rule(seasonal={"lookback": 4},
                              significance={"require": ""})) == []

    def test_confidence_condition_requires_the_declaration(self):
        issues = lint_rule(rule(conditions=["confidence > 0.5"]))
        assert any("only exists when 'significance' is declared" in i
                   for i in issues)
        assert lint_rule(rule(
            seasonal={}, significance={"require": ""},
            conditions=["confidence > 0.5"])) == []

    def test_min_confidence_without_require_is_advisory(self):
        doc = parse_document({"rules": [
            {**MINIMAL, "seasonal": {},
             "significance": {"min_confidence": 0.5}}]})
        assert lint_document(doc) == []
        assert any("only enforced with require" in i for i in lint_advisories(doc))


class TestRuleRev:
    def test_stable_across_parses_and_key_order(self):
        a = parse_rule(V3)
        b = parse_rule({k: V3[k] for k in reversed(list(V3))})
        assert rule_rev(a) == rule_rev(b)
        assert len(rule_rev(a)) == 16 and rule_rev(a) == rule_rev(a)

    def test_changes_only_with_the_rule_itself(self):
        """N2:rule_digest 是整份文件的字节哈希,改 B 规则会污染 A 规则
        的回评分桶;rule_rev 只认这一条。"""
        a = parse_rule(V3)
        a2 = parse_rule(V3)
        other_edited = parse_rule({**V3, "id": "other", "name": "改过了"})
        assert rule_rev(a) == rule_rev(a2)
        assert rule_rev(a) != rule_rev(other_edited)

    def test_changes_when_the_rule_changes(self):
        base = parse_rule(V3)
        assert rule_rev(base) != rule_rev(rule(conditions=["delta < 0"],
                                               seasonal={}, significance={}))
        assert rule_rev(base) != rule_rev(
            parse_rule({**V3, "seasonal": {"lookback": 13}}))


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
