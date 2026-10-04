"""因果升级梯纯函数单测 —— 手算可验、算不出 → None + reason、条件全可判定。

三条纪律各有对应用例:①条件不足诚实降级且 ``unmet`` 带实测值与阈值;
②算不出 ≠ 平行(placebo 做不出 → 不升级);③``assumptions`` 永远非空,
「无法检验」的条目 ``checked=None`` 出现而不是省略。
"""

from __future__ import annotations

import pytest

from trove.services.decision.causal import (
    CROSSES_ZERO_BAND,
    LADDER_ASSUMPTIONS,
    assumptions_for,
    causal_line,
    crosses_zero,
    did_2x2,
    did_se,
    donor_counterfactual,
    ladder_decision,
    parallel_trends,
    placebo_pairs,
)


class TestDid2x2:
    def test_hand_computed(self):
        out = did_2x2(100, 130, 50, 60)
        assert out == {"att": 20.0, "treated_delta": 30.0, "control_delta": 10.0}

    def test_missing_or_non_numeric_is_none(self):
        for args in [(None, 130, 50, 60), (100, 130, None, 60),
                     (100, "x", 50, 60), ("", 130, 50, 60), (True, 1, 1, 1)]:
            assert did_2x2(*args) is None

    def test_negative_att(self):
        assert did_2x2(100, 90, 50, 55)["att"] == -15.0


class TestPlaceboPairs:
    def test_adjacent_pairs_hand_computed(self):
        pairs = placebo_pairs([100, 110, 120, 130], [50, 55, 60, 65])
        assert [p["did"] for p in pairs] == [5.0, 5.0, 5.0]

    def test_count_keeps_the_most_recent(self):
        treated = [100, 110, 120, 130, 140]
        control = [50, 55, 60, 65, 70]
        pairs = placebo_pairs(treated, control, count=2)
        assert len(pairs) == 2

    def test_incomplete_blocks_are_skipped_not_zero_filled(self):
        pairs = placebo_pairs([100, None, 120, 130], [50, 55, 60, 65])
        # 相邻对 (0,1)、(1,2) 都碰到缺值 → 跳过(不补零、不桥接);
        # 只剩 (2,3) 一对 —— 宁可少一对,不造可伪的平行证据
        assert [p["did"] for p in pairs] == [(130 - 120) - (65 - 60)]

    def test_window_labels_ride_along_when_blocks_given(self):
        blocks = [("b1", "b1"), ("b2", "b2"), ("b3", "b3")]
        pairs = placebo_pairs([1, 2, 3], [1, 2, 3], blocks)
        assert pairs[-1]["window"] == ["b3", "b3"]

    def test_fewer_than_two_blocks_is_no_pairs(self):
        assert placebo_pairs([100], [50]) == []


class TestParallelTrends:
    def test_passes_within_the_tolerance(self):
        pairs = placebo_pairs([100, 110, 120], [50, 55, 60])
        out = parallel_trends(pairs, scale=110, tolerance=0.1)
        assert out["passes"] is True and out["reason"] == ""
        assert out["max_abs_did"] == 5.0
        assert out["threshold"] == pytest.approx(11.0)

    def test_failure_carries_measured_and_threshold(self):
        pairs = [{"did": 15.0}, {"did": -3.0}]
        out = parallel_trends(pairs, scale=110, tolerance=0.1)
        assert out["passes"] is False
        assert out["reason"] == "placebo_not_parallel"
        assert out["max_abs_did"] == 15.0
        assert out["threshold"] == pytest.approx(11.0)

    def test_no_pairs_is_not_parallel(self):
        out = parallel_trends([], scale=110, tolerance=0.1)
        assert out["passes"] is False and out["reason"] == "no_placebo_pairs"

    def test_zero_scale_refuses_a_relative_tolerance(self):
        out = parallel_trends([{"did": 1.0}], scale=0.0, tolerance=0.1)
        assert out["passes"] is False and out["reason"] == "zero_scale"
        assert parallel_trends([{"did": 1.0}], scale=None, tolerance=0.1)[
            "reason"] == "zero_scale"

    def test_bad_tolerance_falls_back_to_the_default(self):
        out = parallel_trends([{"did": 15.0}], scale=110, tolerance="x")
        assert out["tolerance"] == pytest.approx(0.1) and out["passes"] is False


class TestDidSe:
    def test_ddof_one_dispersion(self):
        assert did_se([{"did": 4.0}, {"did": 6.0}]) == pytest.approx(2 ** 0.5)

    def test_zero_dispersion_is_zero_not_none(self):
        assert did_se([{"did": 5.0}, {"did": 5.0}, {"did": 5.0}]) == 0.0

    def test_fewer_than_two_pairs_is_none(self):
        assert did_se([]) is None
        assert did_se([{"did": 1.0}]) is None

    def test_crosses_zero_uses_the_two_se_band(self):
        assert crosses_zero(3.0, 2.0) is True          # 3 ≤ 4
        assert crosses_zero(5.0, 2.0) is False         # 5 > 4
        assert crosses_zero(0.0, 0.0) is True          # 零效应不跨出零带
        assert crosses_zero(1.0, None) is None
        assert crosses_zero(None, 1.0) is None
        assert CROSSES_ZERO_BAND == 2.0


class TestDonorCounterfactual:
    def test_exact_tracking_gives_zero_fit_and_hand_computed_effect(self):
        out = donor_counterfactual([100, 110, 120, 160], {"D": [50, 55, 60, 90]})
        assert out["fit_relative"] == pytest.approx(0.0, abs=1e-12)
        assert out["fit_passes"] is True and out["reason"] == ""
        assert out["counterfactual"] == pytest.approx(180.0)
        assert out["effect"] == pytest.approx(-20.0)

    def test_same_ratio_donors_are_scale_invariant(self):
        out = donor_counterfactual(
            [100, 110, 120, 160],
            {"A": [50, 55, 60, 90], "B": [150, 165, 180, 270]})
        assert out["donors"] == ["A", "B"]
        assert out["fit_relative"] == pytest.approx(0.0, abs=1e-12)
        assert out["effect"] == pytest.approx(-20.0)

    def test_poor_fit_refuses_upgrade_with_reason(self):
        out = donor_counterfactual([100, 110, 120, 160], {"D": [50, 55, 120, 90]},
                                   tolerance=0.1)
        assert out["fit_passes"] is False
        assert out["reason"] == "fit_too_poor"
        assert out["fit_relative"] > 0.1

    def test_unusable_donors_are_skipped(self):
        out = donor_counterfactual(
            [100, 110, 120, 160],
            {"good": [50, 55, 60, 90], "empty": [None, None, None, None],
             "nopost": [1, 2, 3, None], "short": [1, 2]})
        assert out["donors"] == ["good"]
        assert out["skipped"] == ["empty", "nopost", "short"]

    def test_no_usable_donor_is_no_donors(self):
        out = donor_counterfactual([100, 110, 120, 160], {"bad": [None] * 4})
        assert out["reason"] == "no_donors" and out["fit_passes"] is False

    def test_zero_levels_refuse(self):
        assert donor_counterfactual([0, 0, 0, 10], {"D": [1, 2, 3, 4]})[
            "reason"] == "zero_scale"
        assert donor_counterfactual([100, 110, 120, 160], {"D": [0, 0, 0, 1]})[
            "reason"] == "zero_scale"

    def test_missing_post_or_too_few_blocks(self):
        assert donor_counterfactual([100, 110, 120, None], {"D": [1, 2, 3, 4]})[
            "reason"] == "no_post_value"
        assert donor_counterfactual([100, 160], {"D": [1, 2]})[
            "reason"] == "insufficient_blocks"
        # 历史只有一格有值 → 拟合对 <2
        assert donor_counterfactual([100, None, None, 160],
                                    {"D": [1, 2, 3, 4]})[
            "reason"] == "insufficient_blocks"


_PLACEBO_OK = {"passes": True, "reason": "", "max_abs_did": 5.0,
               "threshold": 11.0, "count": 4}
_PLACEBO_BAD = {"passes": False, "reason": "placebo_not_parallel",
                "max_abs_did": 15.0, "threshold": 11.0, "count": 4}
_FIT_OK = {"fit_passes": True, "reason": "", "fit_relative": 0.0,
           "tolerance": 0.1, "donors": ["A"]}
_FIT_BAD = {"fit_passes": False, "reason": "fit_too_poor", "fit_relative": 0.3,
            "tolerance": 0.1, "donors": ["A"]}


def _ladder(**overrides):
    kwargs = dict(seasonal_declared=True, n_blocks=12, has_pre_block=True,
                  has_control=True, budget_ok=True, placebo=_PLACEBO_OK,
                  mode="auto", synthetic=_FIT_OK)
    kwargs.update(overrides)
    return ladder_decision(**kwargs)


class TestLadderDecision:
    def test_full_pass_reaches_l3(self):
        out = _ladder()
        assert out == {"rung": "L3", "unmet": []}

    def test_did_mode_caps_at_l2_and_ignores_synthetic(self):
        out = _ladder(mode="did")
        assert out["rung"] == "L2" and out["unmet"] == []

    def test_no_seasonal_is_c4(self):
        out = _ladder(seasonal_declared=False)
        assert out["rung"] == "L1"
        assert out["unmet"][0] == {"condition": "C4", "reason": "no_seasonal"}

    def test_no_pre_block_is_c4(self):
        assert _ladder(has_pre_block=False)["unmet"][0]["reason"] == "no_pre_block"

    def test_insufficient_blocks_is_c1_with_measured_and_threshold(self):
        out = _ladder(n_blocks=7)
        assert out["rung"] == "L1"
        assert out["unmet"][0] == {"condition": "C1",
                                   "reason": "insufficient_blocks",
                                   "measured": 7, "threshold": 8}

    def test_control_missing_is_c2(self):
        out = _ladder(has_control=False)
        assert out["unmet"][0] == {"condition": "C2",
                                   "reason": "control_unavailable"}

    def test_budget_yield_is_c5(self):
        out = _ladder(budget_ok=False)
        assert out["unmet"][0] == {"condition": "C5",
                                   "reason": "query_budget_exceeded"}

    def test_unverifiable_placebo_does_not_upgrade(self):
        """算不出 ≠ 平行:placebo 做不出一律不升级,理由单列。"""
        out = _ladder(placebo=None)
        assert out["rung"] == "L1"
        assert out["unmet"][0] == {"condition": "C3",
                                   "reason": "placebo_unverifiable"}

    def test_failed_placebo_is_c3_with_measured_and_threshold(self):
        out = _ladder(placebo=_PLACEBO_BAD)
        assert out["rung"] == "L1"
        assert out["unmet"][0] == {"condition": "C3",
                                   "reason": "placebo_not_parallel",
                                   "measured": 15.0, "threshold": 11.0}

    def test_poor_fit_stops_honestly_at_l2(self):
        out = _ladder(synthetic=_FIT_BAD)
        assert out["rung"] == "L2"
        assert out["unmet"][0] == {"condition": "C6", "reason": "fit_too_poor",
                                   "measured": 0.3, "threshold": 0.1}

    def test_missing_synthetic_is_reported_not_silent(self):
        out = _ladder(synthetic=None, mode="auto")
        assert out["rung"] == "L2"
        assert out["unmet"][0] == {"condition": "C6",
                                   "reason": "synthetic_unavailable"}

    def test_deterministic(self):
        assert _ladder() == _ladder()


class TestAssumptions:
    def test_l1_lists_why_and_never_claims_causality(self):
        unmet = [{"condition": "C3", "reason": "placebo_not_parallel",
                  "measured": 15.0, "threshold": 11.0}]
        out = assumptions_for("L1", unmet=unmet)
        assert out and out[0]["checked"] is None
        assert "不主张因果" in out[0]["text"]
        assert "placebo_not_parallel" in out[0]["detail"]
        assert "15.00" in out[0]["detail"] and "11.00" in out[0]["detail"]

    def test_l2_has_checked_and_uncheckable_entries(self):
        out = assumptions_for("L2", placebo=_PLACEBO_OK, context="粒度 month")
        assert len(out) == 3
        assert out[0]["checked"] is True and "max|did|" in out[0]["detail"]
        assert out[-1]["checked"] is None and "无法检验" in out[-1]["text"]
        assert all(entry["detail"] for entry in out)

    def test_l3_reports_donor_fit(self):
        out = assumptions_for("L3", placebo=_PLACEBO_OK, synthetic=_FIT_OK)
        assert len(out) == 4
        assert any("供体" in e["detail"] and "0.10" in e["detail"] for e in out)
        assert [e["checked"] for e in out].count(None) == 1

    def test_every_rung_has_a_nonempty_definition(self):
        for rung, entries in LADDER_ASSUMPTIONS.items():
            assert entries, rung
        assert assumptions_for("garbage")[0]["text"] == \
            LADDER_ASSUMPTIONS["L1"][0][0]


class TestCausalLine:
    def test_l1_never_rides_in_the_message(self):
        assert causal_line({"rung": "L1"}) == ""
        assert causal_line(None) == ""
        assert causal_line({}) == ""

    def test_l2_formats_effect_and_design(self):
        section = {"rung": "L2",
                   "did": {"att": 20.0, "crosses_zero": False}}
        assert causal_line(section) == "净效应：+20.00（DiD·区间不跨零）"
        section["did"]["crosses_zero"] = True
        assert causal_line(section) == "净效应：+20.00（DiD·区间跨零）"

    def test_unknown_noise_estimate_is_stated(self):
        section = {"rung": "L2", "did": {"att": -5.0, "crosses_zero": None}}
        assert causal_line(section) == "净效应：-5.00（DiD·噪声估计不足）"

    def test_l3_uses_the_synthetic_effect(self):
        section = {"rung": "L3",
                   "did": {"att": 20.0, "crosses_zero": False},
                   "synthetic": {"effect": -12.31, "crosses_zero": False}}
        assert causal_line(section) == "净效应：-12.31（合成对照·区间不跨零）"

    def test_missing_effect_does_not_render(self):
        assert causal_line({"rung": "L2", "did": {"att": None}}) == ""
        assert causal_line({"rung": "L3", "synthetic": {}}) == ""
