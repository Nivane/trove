"""Decision condition language — parse-time strictness + Kleene evaluation.

The two properties worth guarding hardest:

* **Closed scope.** A misspelled identifier or an unregistered function must
  be a *parse* error. If it silently became Unknown, a rule with a typo
  would simply never fire and nobody would notice.
* **Three-valued logic.** Two-valued logic turns `not (x > y)` into a
  trigger when the baseline is missing — a false alarm on absent data. The
  truth tables below pin the Kleene behaviour.
"""

import math
from decimal import Decimal

import pytest

from trove.services.decision.expr import (
    UNKNOWN,
    DecisionExprError,
    as_number,
    condition_variables,
    evaluate_condition,
    parse_condition,
)

SCOPE = {
    "current": 80.0,
    "baseline": 100.0,
    "delta": -20.0,
    "delta_pct": -0.2,
    "contribution": -0.35,
    "row_count": 12,
    "dim": "华东",
}


def ev(text, **overrides):
    scope = dict(SCOPE)
    scope.update(overrides)
    return evaluate_condition(text, scope)


# ── basics ───────────────────────────────────────────────────

class TestBasics:
    def test_comparison(self):
        assert ev("delta < 0") is True
        assert ev("delta > 0") is False

    def test_all_six_operators(self):
        assert ev("delta == -20") is True
        assert ev("delta != 0") is True
        assert ev("delta <= -20") is True
        assert ev("delta >= -20") is True
        assert ev("delta < -20") is False
        assert ev("delta > -20") is False

    def test_negative_literals_and_unary_minus(self):
        assert ev("delta_pct < -0.1") is True
        assert ev("-delta > 0") is True
        assert ev("delta < -delta_pct") is True   # -20 < 0.2

    def test_no_arithmetic_operators(self):
        """`delta - 5` is a parse error, not a silently-misparsed expression:
        the language has no arithmetic, so pointing the author at `delta` is
        more useful than guessing."""
        with pytest.raises(DecisionExprError):
            parse_condition("delta - 5 < 0")

    def test_string_equality_on_dimension(self):
        assert ev('dim == "华东"') is True
        assert ev("dim == '华北'") is False
        assert ev('dim != "华北"') is True

    def test_parentheses_group(self):
        assert ev("(delta < 0 or row_count > 100) and dim == \"华东\"") is True
        assert ev("(delta < 0 or row_count > 100) and dim == \"华北\"") is False

    def test_and_or_precedence_and_binds_tighter(self):
        # False and False or True  →  (False and False) or True  →  True
        assert ev("delta > 0 and row_count > 100 or current == 80") is True

    def test_variables_reported_for_lint(self):
        assert condition_variables("delta_pct < -0.1 and abs(delta) > 100") == {
            "delta_pct", "delta"}


# ── functions ────────────────────────────────────────────────

class TestFunctions:
    def test_abs(self):
        assert ev("abs(delta) > 10") is True
        assert ev("abs(delta) > 100") is False

    def test_min_max(self):
        assert ev("min(delta, 0) == -20") is True
        assert ev("max(delta, 0) == 0") is True
        assert ev("min(current, baseline, delta) == -20") is True

    def test_pct_change(self):
        assert ev("pct_change(current, baseline) < -0.1") is True
        assert ev("pct_change(current, baseline) == delta_pct") is True

    def test_pct_change_zero_denominator_is_unknown_not_infinity(self):
        """Zero denominator must be Unknown, matching the compiler's
        NULLIF(x,0) convention — not inf (which would make `<` vacuously
        true and fire the rule)."""
        assert ev("pct_change(current, 0) < 0", ) is False
        assert ev("not (pct_change(current, 0) < 0)") is False

    def test_nested_calls(self):
        assert ev("abs(min(delta, -100)) == 100") is True


# ── parse-time strictness (closed scope) ─────────────────────

class TestParseErrors:
    @pytest.mark.parametrize("text", [
        "dleta < 0",                     # typo'd identifier
        "revenue < 0",                   # not in the closed variable set
        "trend(3) > 0",                  # reserved but not registered
        "contains(dim, 'x')",            # unknown function
        "abs(1, 2) == 1",                # wrong arity
        "min(1) == 1",                   # min needs two
        "delta",                         # bare operand, no comparison
        "delta < 0 and",                 # trailing operator
        "(delta < 0",                    # unbalanced paren
        "delta < 0)",                    # trailing junk
        "delta < ",                      # missing right operand
        "",                              # empty
        "delta $ 0",                     # unexpected character
    ])
    def test_rejected(self, text):
        with pytest.raises(DecisionExprError):
            parse_condition(text)

    def test_unknown_identifier_names_the_alternatives(self):
        """An author reading the error should not have to open the source."""
        with pytest.raises(DecisionExprError) as ei:
            parse_condition("dleta > 0")
        msg = str(ei.value)
        assert "dleta" in msg and "delta" in msg and "available" in msg

    def test_parse_errors_are_not_cached(self):
        """A rule being edited must stay fixable — the lru_cache only holds
        successful parses, so a corrected expression re-parses."""
        with pytest.raises(DecisionExprError):
            parse_condition("dleta > 0")
        assert parse_condition("delta > 0") is not None


# ── Kleene three-valued logic ────────────────────────────────

class TestKleeneLogic:
    """`missing` makes every variable Unknown, standing in for a rule whose
    baseline window returned no data."""

    MISSING = {"baseline": None, "delta": None, "delta_pct": None}

    def test_unknown_comparison_does_not_trigger(self):
        assert ev("delta < 0") is True
        assert ev("delta < 0", **self.MISSING) is False

    def test_not_unknown_stays_unknown(self):
        """The false-positive this whole module exists to prevent: with two
        values, `not (x > y)` on missing data would fire."""
        assert ev("not (delta < 0)", **self.MISSING) is False
        assert ev("not (delta > 0)", **self.MISSING) is False

    def test_unknown_and_false_is_false(self):
        """False is decisive under AND even with an Unknown sibling."""
        assert ev("delta < 0 and current == 999", **self.MISSING) is False

    def test_unknown_and_true_is_unknown(self):
        assert ev("delta < 0 and current == 80", **self.MISSING) is False

    def test_unknown_or_true_is_true(self):
        """True is decisive under OR even with an Unknown sibling."""
        assert ev("delta < 0 or current == 80", **self.MISSING) is True

    def test_unknown_or_false_is_unknown(self):
        assert ev("delta < 0 or current == 999", **self.MISSING) is False

    def test_unknown_is_not_a_trigger_value(self):
        node = parse_condition("delta < 0")
        assert node.eval({"delta": None}) is UNKNOWN
        with pytest.raises(TypeError):
            bool(UNKNOWN)

    def test_type_mismatch_is_unknown_not_false(self):
        """`dim > 5` must not become "definitely false" — under `not` that
        would flip into a trigger."""
        assert ev("dim > 5") is False
        assert ev("not (dim > 5)") is False

    def test_nan_is_unknown(self):
        """A NULL that reached the engine as NaN must not compare as False."""
        assert ev("current > 0", current=float("nan")) is False
        assert ev("not (current > 0)", current=float("nan")) is False


# ── numeric coercion ─────────────────────────────────────────

class TestAsNumber:
    def test_plain_types(self):
        assert as_number(3) == 3.0
        assert as_number(3.5) == 3.5
        assert as_number(Decimal("12.50")) == 12.5

    def test_strings_from_sqlite(self):
        assert as_number("12.5") == 12.5
        assert as_number("1,234.5") == 1234.5
        assert as_number("¥1200") == 1200.0
        assert as_number("35%") == 35.0

    def test_unparseable_is_none_not_zero(self):
        """0.0 would be a confident "not triggered"; None becomes Unknown."""
        assert as_number("N/A") is None
        assert as_number("") is None
        assert as_number(None) is None
        assert as_number(True) is None       # bool is not a number here
        assert as_number(float("nan")) is None
        assert as_number(float("inf")) is None

    def test_scope_uses_coercion(self):
        assert ev("current > 10", current="80.5") is True
        assert ev("current > 10", current="N/A") is False
        assert math.isclose(SCOPE["delta_pct"], -0.2)


# ── parameterised identifier closed sets ─────────────────────

def test_parameterised_variables_reject_foreign_identifier():
    """validator 域的词在决策域里仍须被拒 —— 闭集不许因为参数化而变宽。"""
    from trove.services.decision.expr import VALIDATOR_VARIABLES

    node = parse_condition("min >= 0", VALIDATOR_VARIABLES)
    assert node is not None
    assert node.identifiers() == {"min"}

    with pytest.raises(DecisionExprError):
        parse_condition("delta > 0", VALIDATOR_VARIABLES)
    with pytest.raises(DecisionExprError):
        parse_condition("min >= 0")          # 决策域不认识 min


def test_default_variables_unchanged():
    """不传新参数时与今天逐字等价 —— 参数化是一次重构,不是语义变更。"""
    assert condition_variables("delta > 0 and current < baseline") == {
        "delta", "current", "baseline",
    }
    assert evaluate_condition("delta > 0", {"delta": 1.0}) is True


def test_min_is_both_identifier_and_function():
    """``min`` 同时是聚合标量与 FUNCTIONS 里的函数名:两种拼法都必须能解析。

    消歧靠语法而非命名 —— 后面跟 ``(`` 是调用,否则是标识符。这条不是
    巧合而是必须保证的:管理员写 ``min >= 0`` 是读聚合值,写 ``min(0, x)``
    是取最小值。测试钉住这个共存的合法性。
    """
    from trove.services.decision.expr import VALIDATOR_VARIABLES

    assert parse_condition("min >= 0", VALIDATOR_VARIABLES).identifiers() == {"min"}
    assert parse_condition("min(0, 5) == 0", VALIDATOR_VARIABLES) is not None


def test_validator_scope_missing_column_is_unknown_not_false():
    """缺列 → 求值为 Unknown,且 Unknown 不许被读成假值。"""
    from trove.services.decision.expr import UNKNOWN, VALIDATOR_VARIABLES

    node = parse_condition("min >= 0", VALIDATOR_VARIABLES)
    got = node.eval({"row_count": 3.0})
    assert got is UNKNOWN
    with pytest.raises(TypeError):
        bool(got)


# ── 一致性回归门:FUNCTIONS ↔ Call.eval ──────────────────────

def test_call_eval_branches_match_functions_table():
    """函数表(解析期闭集)与求值分支必须同键集。

    ``FUNCTIONS`` 决定 `f(...)` 能否解析,``Call.eval`` 决定它能否算出来。
    两边漂移的失败模式是**最坏的一种**:解析放行、求值落 ``UNKNOWN`` ——
    一条规则从"写错了"变成"永远判不了",而判不了永远不触发,从外面看与
    "检查通过"一模一样。键集相等 = 两个表在同一份代码里。
    """
    import inspect
    import re

    from trove.services.decision.expr import FUNCTIONS, Call

    src = inspect.getsource(Call.eval)
    branches = set(re.findall(r'self\.name == "([a-z_][a-z0-9_]*)"', src))
    assert branches == set(FUNCTIONS)
    # 兜底仍在:未知名字不许有除 Unknown 之外的结局(表与分支相等时它
    # 是死代码,但它是"两边漂移时唯一的安全网")。
    assert src.rstrip().endswith("return UNKNOWN")
