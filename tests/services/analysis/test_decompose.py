"""分析服务纯数学单测:数字逐一对齐旧 attribution 行为。

这些用例与新 ``trove/services/analysis/decompose.py`` 一起钉住搬迁:
``tests/workflow/test_attribution.py`` 保证节点行为不变,这里保证
数学原语在服务面上的契约(含新增的 ``residual``)。
"""

import pytest

from trove.services.analysis.decompose import (
    breakdown_signal,
    contribution,
    num,
    ratio_share,
    residual,
    shift_share,
    signed_children,
)


class TestNum:
    def test_scalar_and_string_forms(self):
        assert num(3) == 3.0
        assert num(3.5) == 3.5
        assert num("1,234.5") == 1234.5
        assert num("¥100") == 100.0
        assert num("12%") == 12.0
        assert num(None) == 0.0
        assert num("abc") == 0.0
        # bool 是 int 子类,但不当数字用
        assert num(True) == 0.0


class TestContribution:
    def test_signed_contribution_sums_over_abs(self):
        rows = contribution({"a": 100, "b": 200}, {"a": 150, "b": 160})
        assert [r["dim"] for r in rows] == ["a", "b"]
        assert rows[0]["delta"] == 50
        assert rows[0]["contribution"] == pytest.approx(50 / 90)
        assert rows[1]["contribution"] == pytest.approx(-40 / 90)

    def test_zero_total_falls_back_to_share(self):
        rows = contribution({"a": 100, "b": 100}, {"a": 100, "b": 100})
        assert rows[0]["contribution"] == pytest.approx(0.5)

    def test_missing_keys_count_as_zero(self):
        rows = contribution({"a": 10}, {"b": 4})
        by = {r["dim"]: r for r in rows}
        assert by["a"]["current"] == 0.0 and by["a"]["delta"] == -10
        assert by["b"]["base"] == 0.0 and by["b"]["delta"] == 4


class TestShiftShare:
    def test_three_effects_sum_to_delta(self):
        dec = shift_share({"A": (100, 10), "B": (90, 10)}, {"A": (120, 10), "B": (90, 15)})
        e = dec["effects"]
        assert e["within"] + e["composition"] + e["interaction"] == pytest.approx(e["delta"])
        assert sum(r["contribution"] for r in dec["rows"]) == pytest.approx(e["delta"])

    def test_pure_mix_shift_zero_within(self):
        base = {"A": (10, 10), "B": (9, 10)}
        cur = {"A": (20, 20), "B": (9, 10)}
        dec = shift_share(base, cur)
        assert dec["effects"]["within"] == pytest.approx(0.0)

    def test_empty(self):
        dec = shift_share({}, {})
        assert dec["rows"] == [] and dec["base_total"] == 0.0 and dec["cur_total"] == 0.0


class TestRatioShare:
    def test_numerator_share(self):
        dec = ratio_share({"A": (120, 10), "B": (90, 15)})
        assert sum(r["contribution"] for r in dec["rows"]) == pytest.approx(1.0)
        assert dec["cur_total"] == pytest.approx(210 / 25)

    def test_empty(self):
        assert ratio_share({})["rows"] == []


class TestBreakdownSignal:
    def test_additive_signal_is_sum_abs_delta(self):
        sig = breakdown_signal({"a": 10, "b": 5}, {"a": 7, "b": 5}, None)
        assert sig == pytest.approx(3.0)

    def test_ratio_signal_uses_shift_share(self):
        sig = breakdown_signal({"A": (120, 10)}, {"A": (100, 10)}, ("SUM(x)", "COUNT(x)"))
        assert sig == pytest.approx(2.0)

    def test_empty(self):
        assert breakdown_signal({}, {}, None) == 0.0


class TestSignedChildren:
    def test_subtraction_flips_second_term(self):
        assert signed_children("-", [20.0, 10.0]) == [20.0, -10.0]

    def test_subtraction_then_residual_is_exact(self):
        # Δ(revenue − expense) = 20 − 10 = 10 → 符号正确的贡献之和精确
        r = residual(10.0, signed_children("-", [20.0, 10.0]))
        assert r["exact"] is True

    def test_addition_unchanged(self):
        assert signed_children("+", [20.0, 10.0]) == [20.0, 10.0]

    def test_other_ops_unchanged(self):
        assert signed_children("*", [2.0, 3.0]) == [2.0, 3.0]
        assert signed_children(None, [1.0]) == [1.0]


class TestResidual:
    def test_additive_identity_exact(self):
        r = residual(30.0, [18.0, 12.0])
        assert r["exact"] is True
        assert r["value"] == pytest.approx(0.0)

    def test_non_additive_leaves_gap(self):
        r = residual(10.0, [4.0, 3.0])
        assert r["exact"] is False
        assert r["value"] == pytest.approx(3.0)

    def test_float_noise_within_tolerance(self):
        r = residual(0.3, [0.1, 0.2])
        assert r["exact"] is True
