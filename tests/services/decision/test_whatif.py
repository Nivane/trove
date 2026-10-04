"""what-if 模拟的纯函数测试(零查询、零 LLM)。

三条被测试钉死的纪律:

  · **重放保真** —— 从 verdict 行卡重建的 maps 喂回同一个 ``judge``,
    原样判定必须复现行卡里的 ``triggered``(模拟器与判定器不漂移的
    全部保证就在这一条上);
  · **没算的要写出来** —— 未应用的调整带 ``reason``、不可分解节点带
    ``not_modeled``、``total=None`` 与「总变化是 0」在字段上分得开;
  · **带只重放不重算** —— 有带位数据就原样参与,没有就不施加门并
    在 ``degraded`` 里说明(假装带未确认会把一切判成不触发)。

判定内核是 ``service.judge``(B4 从 ``_judge`` 提为模块级纯函数)——
本文件同时是那次提取的回归面:``simulate_rule`` 的 before 与 verdict
行卡一致,就说明两处走的是同一条内核。
"""

from __future__ import annotations

import pytest

from trove.services.decision.rules import Seasonal, Significance, parse_rule
from trove.services.decision.whatif import (
    Adjustment,
    WhatIfError,
    apply_adjustments,
    impact_summary,
    parse_scenario,
    simulate_rule,
    simulate_tree,
)


def _rule(**over) -> object:
    data = {
        "id": "loan-drop",
        "name": "贷款余额环比下滑",
        "window": "本月",
        "subject": {"metrics": ["loan_balance"], "dimensions": ["region"]},
        "baseline": {"kind": "prev_period"},
        "scope": "per_dimension",
        "conditions": ["delta_pct < -0.1"],
    }
    data.update(over)
    return parse_rule(data)


#: 华东 −50%(触发),华北 +12.5%(不触发)。
CUR = {"华东": 600.0, "华北": 450.0}
BASE = {"华东": 1200.0, "华北": 400.0}
ROWS = 2


class TestParseScenario:
    def test_empty_scenario_is_legal(self):
        assert parse_scenario(None) == []
        assert parse_scenario([]) == []

    def test_full_form(self):
        out = parse_scenario([{"dim": "华东", "field": "baseline",
                               "mode": "abs", "value": -100}])
        assert out == [Adjustment(dim="华东", field="baseline", mode="abs",
                                  value=-100.0)]

    def test_defaults_are_current_side_relative_change(self):
        out = parse_scenario([{"dim": "华东", "value": -0.1}])
        assert out[0].field == "current" and out[0].mode == "pct"

    def test_aggregate_adjustment_needs_no_dim(self):
        out = parse_scenario([{"value": 0.05}])
        assert out[0].dim == ""

    @pytest.mark.parametrize("bad", [
        {"dim": "华东", "value": 1, "mode": "pct", "extra": 1},
        {"dim": "华东", "value": 1, "field": "future"},
        {"dim": "华东", "value": 1, "mode": "multiply"},
        {"dim": "华东", "value": "10%"},
        {"dim": "华东", "value": True},
        {"dim": "华东"},
    ])
    def test_malformed_adjustments_are_rejected_not_ignored(self, bad):
        with pytest.raises(WhatIfError):
            parse_scenario([bad])

    def test_unknown_key_error_lists_the_legal_ones(self):
        with pytest.raises(WhatIfError) as e:
            parse_scenario([{"dim": "华东", "value": 1, "modex": "pct"}])
        assert "dim, field, mode, value" in str(e.value)

    def test_scenario_must_be_a_list_of_mappings(self):
        with pytest.raises(WhatIfError):
            parse_scenario({"dim": "华东"})
        with pytest.raises(WhatIfError):
            parse_scenario(["华东"])


class TestApplyAdjustments:
    def test_pct_compounds_off_the_current_value(self):
        cur, base, applied, un = apply_adjustments(
            [Adjustment(dim="华东", field="current", mode="pct", value=-0.5)],
            CUR, BASE)
        assert cur["华东"] == 300.0 and base == BASE
        assert applied[0]["before"] == 600.0 and applied[0]["after"] == 300.0
        assert un == []

    def test_abs_and_set(self):
        cur, _, applied, _ = apply_adjustments(
            [Adjustment(dim="华北", mode="abs", value=+50)], CUR, BASE)
        assert cur["华北"] == 500.0
        cur2, _, _, _ = apply_adjustments(
            [Adjustment(dim="华北", mode="set", value=1.0)], CUR, BASE)
        assert cur2["华北"] == 1.0
        assert applied[0]["mode"] == "abs"

    def test_baseline_side_is_adjusted_independently(self):
        cur, base, _, _ = apply_adjustments(
            [Adjustment(dim="华东", field="baseline", mode="pct", value=0.5)],
            CUR, BASE)
        assert base["华东"] == 1800.0 and cur["华东"] == 600.0

    def test_input_maps_are_not_mutated(self):
        apply_adjustments([Adjustment(dim="华东", value=-0.5)], CUR, BASE)
        assert CUR["华东"] == 600.0 and BASE["华东"] == 1200.0

    def test_unknown_dim_is_unapplied_with_its_reason(self):
        cur, _, applied, un = apply_adjustments(
            [Adjustment(dim="华南", value=-0.5)], CUR, BASE)
        assert applied == [] and cur == CUR
        assert un[0]["reason"] == "unknown_dim" and un[0]["dim"] == "华南"

    def test_a_value_that_is_none_cannot_be_pct_adjusted(self):
        """缺值算不动 → unapplied;绝不把它当 0 算(那是编数)。"""
        cur, _, applied, un = apply_adjustments(
            [Adjustment(dim="华东", value=-0.5)],
            {"华东": None, "华北": 450.0}, BASE)
        assert applied == [] and un[0]["reason"] == "missing_value"
        assert cur["华东"] is None

    def test_set_works_on_a_missing_value(self):
        cur, _, applied, un = apply_adjustments(
            [Adjustment(dim="华东", mode="set", value=100.0)],
            {"华东": None}, BASE)
        assert cur["华东"] == 100.0 and un == []
        assert applied[0]["before"] is None

    def test_adjustments_apply_in_order(self):
        """第二条看到的是第一条之后的值 —— 场景是执行序,不是集合。"""
        cur, _, _, _ = apply_adjustments(
            [Adjustment(dim="华东", mode="pct", value=0.5),
             Adjustment(dim="华东", mode="abs", value=-100.0)], CUR, BASE)
        assert cur["华东"] == 800.0


class TestSimulateRule:
    def test_cleared_flip(self):
        rule = _rule()
        sim = simulate_rule(rule, CUR, BASE, ROWS, [
            Adjustment(dim="华东", mode="set", value=1200.0)])
        assert sim["before"]["triggered"] is True
        assert sim["after"]["triggered"] is False
        assert sim["flip"] == "cleared"

    def test_fired_flip(self):
        rule = _rule()
        # 起点两组都不触发(华东 +8.3%,华北 +12.5%),把华北调到 −25%。
        sim = simulate_rule(rule, {"华东": 1300.0, "华北": 450.0}, BASE, ROWS, [
            Adjustment(dim="华北", mode="set", value=300.0)])
        assert sim["before"]["triggered"] is False
        assert sim["after"]["triggered"] is True
        assert sim["flip"] == "fired"

    def test_no_flip(self):
        rule = _rule()
        sim = simulate_rule(rule, CUR, BASE, ROWS, [
            Adjustment(dim="华东", mode="pct", value=0.1)])
        assert sim["flip"] == "none"

    def test_empty_scenario_is_a_pure_replay(self):
        rule = _rule()
        sim = simulate_rule(rule, CUR, BASE, ROWS)
        assert sim["before"] == sim["after"]
        assert sim["applied"] == [] and sim["unapplied"] == []

    def test_before_reproduces_the_verdict_row_cards(self):
        """重放保真:同一内核 + 同一数字 = 同一行卡(含 contribution 列)。"""
        rule = _rule()
        sim = simulate_rule(rule, CUR, BASE, ROWS)
        rows = {r["dim"]: r for r in sim["before"]["rows"]}
        assert rows["华东"]["triggered"] is True
        assert rows["华东"]["delta_pct"] == pytest.approx(-0.5)
        assert rows["华北"]["triggered"] is False
        assert rows["华东"]["matched"] == ["delta_pct < -0.1"]

    def test_a_rule_without_significance_reports_no_degraded(self):
        sim = simulate_rule(_rule(), CUR, BASE, ROWS)
        assert sim["degraded"] == []

    def test_significance_without_band_data_never_gates_and_says_so(self):
        """没有带位时**不施加门**:假装带未确认会把一切判成不触发,
        读起来像「这条规则永远不响」—— 比不给模拟更糟。"""
        rule = _rule(seasonal={"grain": "month", "lookback": 12,
                               "mode": "trailing", "k": 3.5},
                     significance={"require": "outside_band"})
        sim = simulate_rule(rule, CUR, BASE, ROWS)
        assert sim["before"]["triggered"] is True      # 条件级翻转照常
        assert sim["degraded"] == [{"stage": "significance",
                                    "reason": "not_simulated"}]

    def test_replayed_band_positions_gate_both_runs(self):
        """带位数据在 → 原样参与两次判定,且明说「重放非重算」。"""
        rule = _rule(seasonal={"grain": "month", "lookback": 12,
                               "mode": "trailing", "k": 3.5},
                     significance={"require": "outside_band"})
        sim = simulate_rule(rule, CUR, BASE, ROWS, [
            Adjustment(dim="华东", mode="set", value=1200.0)],
            confidence_by_dim={"华东": 0.93, "华北": 0.0},
            gated_by_dim={"华东": True, "华北": False})
        assert sim["before"]["triggered"] is True
        assert sim["after"]["triggered"] is False
        assert sim["degraded"] == [{"stage": "significance",
                                    "reason": "replayed_not_recomputed"}]

    def test_a_group_that_fires_but_fails_the_replayed_gate_stays_visible(self):
        rule = _rule(seasonal={"grain": "month", "lookback": 12,
                               "mode": "trailing", "k": 3.5},
                     significance={"require": "outside_band"})
        sim = simulate_rule(rule, CUR, BASE, ROWS,
                            confidence_by_dim={"华东": 0.2, "华北": 0.0},
                            gated_by_dim={"华东": False, "华北": False})
        row = {r["dim"]: r for r in sim["before"]["rows"]}["华东"]
        assert row["triggered"] is False and row["gated"] is False
        assert row["matched"] == ["delta_pct < -0.1"]  # 差一点也是审计事实

    def test_aggregate_rules_judge_the_single_dimless_group(self):
        rule = _rule(scope="aggregate",
                     subject={"metrics": ["loan_balance"]},
                     emit="any")
        sim = simulate_rule(rule, {"": 100.0}, {"": 200.0}, 1, [
            Adjustment(mode="set", value=180.0)])
        assert sim["before"]["triggered"] is True
        assert sim["after"]["triggered"] is False
        assert sim["after"]["rows"][0]["dim"] == ""

    def test_unknown_dim_adjustment_leaves_the_run_intact_and_reported(self):
        rule = _rule()
        sim = simulate_rule(rule, CUR, BASE, ROWS, [
            Adjustment(dim="华南", value=-0.9)])
        assert sim["flip"] == "none"
        assert sim["unapplied"][0]["reason"] == "unknown_dim"
        assert sim["applied"] == []


class TestSimulateTree:
    def _leaf(self, name, candidate, decomposable=True):
        return {"name": name, "candidate": candidate, "kind": "leaf",
                "expression": candidate, "decomposable": decomposable,
                "children": []}

    def _node(self, name, op, kids, decomposable):
        return {"name": name, "kind": "derived", "op": op, "decomposable":
                decomposable, "children": kids, "expression": f"({op})"}

    def test_an_additive_tree_sums_to_the_root(self):
        tree = self._node("total", "+", [
            self._leaf("华东", "loan_a"), self._leaf("华北", "loan_b")], True)
        out = simulate_tree(tree, {"loan_a": -100.0, "loan_b": 40.0})
        assert out["total"] == -60.0
        assert out["not_modeled"] == []

    def test_a_difference_node_subtracts(self):
        tree = self._node("net", "-", [
            self._leaf("收入", "rev"), self._leaf("支出", "cost")], True)
        out = simulate_tree(tree, {"rev": 50.0, "cost": 20.0})
        assert out["total"] == 30.0

    def test_a_non_additive_node_is_not_summed(self):
        """比率节点的子项和不是父变化量 —— 标出来,不算出来。"""
        tree = self._node("margin", "/", [
            self._leaf("利润", "p"), self._leaf("收入", "r")], False)
        out = simulate_tree(tree, {"p": 10.0, "r": -10.0})
        assert out["total"] is None
        assert out["not_modeled"] == [{"node": "margin", "op": "/",
                                       "reason": "non_additive",
                                       "detail": "子项变化量之和不等于父变化量"}]

    def test_the_unmodelled_child_pulls_its_parent_down_with_it(self):
        ratio = self._node("margin", "/", [
            self._leaf("利润", "p"), self._leaf("收入", "r")], False)
        tree = self._node("total", "+", [ratio, self._leaf("其他", "o")], True)
        out = simulate_tree(tree, {"p": 1.0, "r": 1.0, "o": 1.0})
        assert out["total"] is None
        reasons = [m["reason"] for m in out["not_modeled"]]
        assert reasons == ["non_additive", "child_not_modeled"]

    def test_a_leaf_without_an_impact_counts_as_assumed_unchanged(self):
        tree = self._node("total", "+", [
            self._leaf("华东", "loan_a"), self._leaf("华北", "loan_b")], True)
        out = simulate_tree(tree, {"loan_a": -100.0})
        assert out["total"] == -100.0
        assert out["assumed_unchanged"] == 1

    def test_impact_keys_match_candidates_case_insensitively(self):
        tree = self._node("total", "+", [self._leaf("华东", "Loan_A")], True)
        assert simulate_tree(tree, {"loan_a": -5.0})["total"] == -5.0

    def test_an_empty_impact_map_is_an_all_unchanged_tree(self):
        tree = self._node("total", "+", [
            self._leaf("华东", "loan_a"), self._leaf("华北", "loan_b")], True)
        out = simulate_tree(tree, {})
        assert out["total"] == 0.0 and out["assumed_unchanged"] == 2


class TestImpactSummary:
    def test_the_line_carries_the_change_and_the_flip(self):
        rule = _rule()
        sim = simulate_rule(rule, CUR, BASE, ROWS, [
            Adjustment(dim="华东", mode="set", value=1200.0)])
        out = impact_summary(sim)
        assert "华东 current" in out["line"]
        assert "触发 是→否" in out["line"]
        assert out["flip"] == "cleared"
        assert out["changed"] == ["华东.current"]

    def test_unapplied_adjustments_are_counted_in_the_line(self):
        sim = simulate_rule(_rule(), CUR, BASE, ROWS, [
            Adjustment(dim="华南", value=-0.5)])
        out = impact_summary(sim)
        assert out["unapplied_count"] == 1
        assert "1 条调整未模拟" in out["line"]

    def test_an_empty_scenario_says_so_instead_of_faking_a_change(self):
        out = impact_summary(simulate_rule(_rule(), CUR, BASE, ROWS))
        assert "空场景" in out["line"]
        assert out["changed"] == []

    def test_a_zero_total_delta_is_not_the_same_as_an_uncomputable_one(self):
        tree = {"total": None, "not_modeled": [], "assumed_unchanged": 0}
        sim = simulate_rule(_rule(), CUR, BASE, ROWS)
        assert impact_summary(sim, tree=tree)["total_delta"] is None
        assert "不可算" in impact_summary(sim, tree=tree)["line"]
        assert impact_summary(sim, tree={"total": 0.0})["total_delta"] == 0.0

    def test_aggregate_label_has_no_dangling_dim_prefix(self):
        rule = _rule(scope="aggregate", subject={"metrics": ["loan_balance"]})
        sim = simulate_rule(rule, {"": 100.0}, {"": 200.0}, 1, [
            Adjustment(mode="set", value=180.0)])
        assert " current" in impact_summary(sim)["line"]
        assert impact_summary(sim)["changed"] == ["current"]


class TestRuleTypes:
    def test_seasonal_and_significance_survive_parse(self):
        rule = _rule(seasonal={"grain": "month", "lookback": 12,
                               "mode": "trailing", "k": 3.5},
                     significance={"require": "outside_band"})
        assert rule.seasonal == Seasonal(grain="month", lookback=12,
                                         mode="trailing", k=3.5)
        assert rule.significance == Significance(require="outside_band")
        assert rule.significance.required() is True
