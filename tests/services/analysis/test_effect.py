"""效果测量单测(纯):三值结局 / 带只由行动前构成 / DiD 不改 ITS 数字。

形态照 ``test_stats.py``:表驱动 + 「算不出 → None/reason」纪律 +
确定性同输入同结果。刻意钉的三件事:

  - ``outside_band=False`` 是**结论**(无可辨识变化),不是失败 ——
    与 ``None``(判不了)必须分得开;
  - 显式传入的带是**权威**(可复算:从证据拿回来的带再判同一条带);
  - ``attribute_effect`` 绝不改动 ITS 的数字(method 升名但 delta/pct 原样)。
"""

from __future__ import annotations

import pytest

from trove.services.analysis import effect as ef
from trove.services.analysis.stats import Band

#: 一组「有正常波动」的行动前块(至少 8 个,避开 insufficient_n 干扰)。
PRE = [100.0, 102.0, 98.0, 101.0, 99.0, 103.0, 97.0, 101.0]


class TestDid2x2:
    def test_hand_computed(self):
        out = ef.did_2x2(100, 130, 200, 210)
        assert out == {"att": 20.0, "treated_delta": 30.0, "control_delta": 10.0}

    def test_negative_att(self):
        # 处理组 +10,对照组 +30 → 净效应 −20(行动"看起来涨"其实跑输对照)
        assert ef.did_2x2(100, 110, 200, 230)["att"] == -20.0

    @pytest.mark.parametrize("missing", range(4))
    def test_missing_any_arm_is_none(self, missing):
        args = [100, 130, 200, 210]
        args[missing] = None
        assert ef.did_2x2(*args) is None

    @pytest.mark.parametrize("bad", ["abc", True, False, object()])
    def test_non_numeric_is_none(self, bad):
        assert ef.did_2x2(100, bad, 200, 210) is None

    def test_numeric_strings_are_accepted(self):
        assert ef.did_2x2("100", "130", "200", "210")["att"] == 20.0


class TestInterruptedDesign:
    def test_basic_values(self):
        body = ef.interrupted_design(PRE, [150.0])
        assert body["method"] == "its"
        assert body["pre"]["n"] == 8
        assert body["pre"]["center"] == pytest.approx(100.5)
        assert body["post"]["center"] == 150.0
        assert body["delta"] == pytest.approx(49.5)
        assert body["pct"] == pytest.approx(49.5 / 100.5)
        assert body["z"] == pytest.approx(
            (150.0 - body["pre"]["center"]) / body["pre"]["scale"])
        assert body["degraded"] == []

    def test_band_is_built_from_pre_only(self):
        """行动后的点绝不进带 —— 混进去是"行动有效"的假阳性来源。"""
        a = ef.interrupted_design(PRE, [150.0])
        b = ef.interrupted_design(PRE, [9999.0])
        assert a["band"] == b["band"]
        assert a["band"]["center"] == pytest.approx(100.5)
        assert a["band"]["n"] == len(PRE)

    def test_pct_uses_absolute_center(self):
        body = ef.interrupted_design([-100.0, -102.0, -98.0], [-50.0])
        assert body["delta"] == pytest.approx(50.0)
        assert body["pct"] == pytest.approx(0.5)   # /abs(-100) 仍是 +50%

    def test_zero_baseline_has_no_pct(self):
        body = ef.interrupted_design([0.0] * 3, [10.0])
        assert body["delta"] == 10.0
        assert body["pct"] is None
        assert "zero_baseline" in body["degraded"]

    def test_no_pre_blocks_is_degraded_not_zero(self):
        body = ef.interrupted_design([], [10.0])
        assert body["pre"]["center"] is None
        assert body["delta"] is None and body["pct"] is None and body["z"] is None
        assert "no_pre_blocks" in body["degraded"]
        assert "no_data" in body["degraded"]       # 带也不可用

    def test_no_post_blocks_keeps_the_band(self):
        body = ef.interrupted_design(PRE, [])
        assert "no_post_blocks" in body["degraded"]
        assert body["post"]["center"] is None
        assert body["delta"] is None
        assert body["band"]["lo"] is not None      # 行动前分布仍然算得出来

    def test_zero_scale_makes_z_none(self):
        body = ef.interrupted_design([10.0] * 8, [50.0])
        assert body["z"] is None
        assert "zero_scale" in body["band"]["degraded"]

    def test_non_numeric_and_none_entries_are_filtered(self):
        body = ef.interrupted_design([100, None, "x", 102, 98, 101, 99, 103,
                                      97, 101], [150])
        assert body["pre"]["n"] == 8

    def test_low_n_flag(self):
        assert ef.interrupted_design(PRE, [150.0])["low_n"] is True   # 8 < 12
        assert ef.interrupted_design(PRE * 2, [150.0])["low_n"] is False

    def test_deterministic(self):
        assert ef.interrupted_design(PRE, [150.0]) == \
            ef.interrupted_design(PRE, [150.0])


class TestMeasureEffect:
    def test_outside_band_true(self):
        body = ef.measure_effect(PRE, [200.0])
        assert body["outside_band"] is True
        assert body["confidence"] is not None and body["confidence"] > 0
        assert body["k"] == ef.EFFECT_K

    def test_outside_band_false_is_a_conclusion(self):
        """行动后无可辨识变化 —— False 是诚实结论,confidence 恰为带缘的 0。"""
        body = ef.measure_effect(PRE, [100.5])     # = pre 中心
        assert body["outside_band"] is False
        assert body["confidence"] == 0.0

    def test_zero_scale_is_none_not_false(self):
        body = ef.measure_effect([10.0] * 8, [50.0])
        assert body["outside_band"] is None
        assert body["confidence"] is None          # 判不了就没有置信可言

    def test_empty_post_is_none_but_band_survives(self):
        body = ef.measure_effect(PRE, [])
        assert body["outside_band"] is None
        assert body["confidence"] is None

    def test_explicit_band_dict_is_authoritative(self):
        """从证据拿回来的带再判 —— 判的是同一条带,不是现建的。"""
        narrow = {"center": 100.0, "scale": 1.0, "lo": 99.0, "hi": 101.0,
                  "n": 8, "method": "robust", "degraded": []}
        assert ef.measure_effect(PRE, [150.0], narrow)["outside_band"] is True
        wide = {**narrow, "lo": -1000.0, "hi": 1000.0}
        assert ef.measure_effect(PRE, [150.0], wide)["outside_band"] is False

    def test_explicit_band_object_is_accepted(self):
        b = Band(center=100.0, scale=1.0, lo=99.0, hi=101.0, n=8)
        assert ef.measure_effect(PRE, [150.0], b)["outside_band"] is True

    def test_k_moves_the_band(self):
        """k 是带的一半宽:k=1 时 ±100.5±scale —— 150 仍在带外;
        但中心 ±k·scale 的关系必须体现在 band 里。"""
        body = ef.measure_effect(PRE, [150.0], k=1.0)
        assert body["k"] == 1.0
        assert body["band"]["lo"] == pytest.approx(
            body["band"]["center"] - body["band"]["scale"])
        assert body["band"]["hi"] == pytest.approx(
            body["band"]["center"] + body["band"]["scale"])


class TestAttributeEffect:
    def test_did_available(self):
        base = ef.measure_effect(PRE, [150.0])
        out = ef.attribute_effect(base, treated_pre=100.5, treated_post=150.0,
                                  control_pre=200.0, control_post=210.0)
        assert out["causal"] == "did"
        assert out["method"] == "its+did"
        assert out["att"] == {"att": 39.5, "treated_delta": 49.5,
                              "control_delta": 10.0}
        # ITS 的数字一个都没被动过(方法名描述主张层级,不改数字)
        assert out["delta"] == base["delta"]
        assert out["pct"] == base["pct"]
        assert out["z"] == base["z"]
        assert out["outside_band"] == base["outside_band"]

    def test_control_missing_degrades_not_raises(self):
        base = ef.measure_effect(PRE, [150.0])
        out = ef.attribute_effect(base, treated_pre=100.5, treated_post=150.0,
                                  control_pre=None, control_post=210.0)
        assert out["causal"] == "unavailable"
        assert out["causal_reason"] == "control_incomplete"
        assert out["att"] is None
        assert out["method"] == "its"
        assert "causal:control_incomplete" in out["degraded"]
        assert out["delta"] == base["delta"]

    def test_input_not_mutated(self):
        base = ef.measure_effect(PRE, [150.0])
        snapshot = {k: v for k, v in base.items()}
        ef.attribute_effect(base, treated_pre=1, treated_post=2,
                            control_pre=3, control_post=4)
        assert base == snapshot
        assert "causal" not in base                # 新键只落在副本上

    def test_degraded_list_is_copied(self):
        base = ef.measure_effect(PRE, [150.0])
        out = ef.attribute_effect(base, treated_pre=None, treated_post=None,
                                  control_pre=None, control_post=None)
        assert out["degraded"] is not base["degraded"]
        assert "causal:control_incomplete" not in base["degraded"]


class TestFormulaSharing:
    def test_did_is_the_same_object_as_the_ladder_side(self):
        """判定侧升级梯与验收侧净效应是字面同一份 2×2(B7 搬下来的)。"""
        from trove.services.decision.causal import did_2x2 as ladder_did

        assert ladder_did is ef.did_2x2
