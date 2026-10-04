"""显著性门纯函数单测 —— 位置分数 / 带载荷 / 门的三值语义 / 通知行。

这个模块是判定侧的**唯一新增判据**,它把「算不出」与「判了在带内」
严格分开:前者 `outside is None` + reason,后者 `outside is False`。
require 语义(算不出 → error run)在 service,这里钉它的输入契约。
"""

from __future__ import annotations

import pytest

from trove.services.analysis.stats import MIN_BLOCKS, ROBUST_Z_THRESHOLD
from trove.services.decision.significance import (
    CONFIDENCE_NOTE,
    band_line,
    build_band_payload,
    confidence_from_margin,
    gate,
)

#: 历史块:中位数 100、MAD 10(scale ≈ 14.826),带 ≈ 100 ± 51.9。
TIGHT = [90.0, 110.0, 90.0, 110.0, 90.0, 110.0, 90.0, 110.0, 90.0, 110.0]
FLAT = [10.0] * 10


class TestConfidenceFromMargin:
    def test_zero_at_the_band_edge_one_at_twice_the_threshold(self):
        k = 3.5
        assert confidence_from_margin(k, k) == 0.0            # 恰在带缘
        assert confidence_from_margin(2 * k, k) == 1.0        # 两倍阈值
        assert confidence_from_margin(-2 * k, k) == 1.0       # 对称(取 |z|)
        assert confidence_from_margin(1.5 * k, k) == pytest.approx(0.5)

    def test_clamped_into_unit_interval(self):
        assert confidence_from_margin(0.0, 3.5) == 0.0        # 带内 → 0,不取负
        assert confidence_from_margin(1e9, 3.5) == 1.0

    def test_uncomputable_is_none_not_zero(self):
        assert confidence_from_margin(None, 3.5) is None
        assert confidence_from_margin(3.5, 0) is None
        assert confidence_from_margin(3.5, -1) is None
        assert confidence_from_margin("x", 3.5) is None        # type: ignore[arg-type]

    def test_the_note_says_it_is_not_a_probability(self):
        # 限定语是导出契约的一部分:任何把 confidence 渲染出去的地方都要
        # 能拿到它,而它必须写明「非概率」。
        assert "非概率" in CONFIDENCE_NOTE and "p 值" in CONFIDENCE_NOTE


class TestBuildBandPayload:
    def test_payload_shape_is_recomputable(self):
        p = build_band_payload(TIGHT, seed_material="ds|rule|dim|2024-01")
        assert sorted(p) == ["band", "bootstrap", "k", "low_n", "n_eff"]
        assert p["band"]["center"] == pytest.approx(100.0)
        assert p["band"]["n"] == 10
        assert p["k"] == ROBUST_Z_THRESHOLD
        assert p["low_n"] is True          # 10 < LOW_N(12)

    def test_seed_material_pins_the_bootstrap(self):
        a = build_band_payload(TIGHT, seed_material="same")
        b = build_band_payload(TIGHT, seed_material="same")
        c = build_band_payload(TIGHT, seed_material="other")
        assert a["bootstrap"] == b["bootstrap"]
        assert a["bootstrap"] != c["bootstrap"]

    def test_empty_history_is_a_degraded_band_not_an_error(self):
        p = build_band_payload([], seed_material="x")
        assert p["band"]["lo"] is None
        assert "no_data" in p["band"]["degraded"]
        assert p["n_eff"] is None


class TestGate:
    def test_outside_and_confident_gates(self):
        payload = build_band_payload(TIGHT, seed_material="s")
        out = gate(TIGHT, 200.0, payload)
        assert out["outside"] is True and out["gated"] is True
        assert out["reason"] == ""
        assert 0.0 < out["confidence"] <= 1.0
        assert out["z"] == pytest.approx((200 - 100) / (10 * 1.4826), rel=1e-6)

    def test_within_band_is_false_not_none(self):
        payload = build_band_payload(TIGHT, seed_material="s")
        out = gate(TIGHT, 105.0, payload)
        assert out["outside"] is False
        assert out["gated"] is False and out["reason"] == "within_band"
        assert out["confidence"] == 0.0     # 位置分数在带缘之内归零

    def test_min_confidence_is_an_additional_knob(self):
        payload = build_band_payload(TIGHT, seed_material="s")
        # |z| ≈ 3.71 for 155 → confidence ≈ 0.06 —— 过带但不过 0.5 的下限
        out = gate(TIGHT, 155.0, payload, min_confidence=0.5)
        assert out["outside"] is True and out["gated"] is False
        assert out["reason"] == "below_min_confidence"
        assert gate(TIGHT, 155.0, payload, min_confidence=0.0)["gated"] is True

    def test_zero_scale_refuses_to_confirm(self):
        payload = build_band_payload(FLAT, seed_material="s")
        out = gate(FLAT, 999.0, payload)
        assert out["outside"] is None and out["gated"] is False
        assert out["reason"] == "zero_scale"      # 算不出 ≠ 在带内
        assert out["z"] is None and out["confidence"] is None

    def test_insufficient_blocks_never_confirms_even_when_outside(self):
        small = TIGHT[:4]                          # MAD 可算,但 n < MIN_BLOCKS
        payload = build_band_payload(small, seed_material="s")
        assert payload["band"]["lo"] is not None   # 带本身算得出来
        out = gate(small, 999.0, payload)
        assert out["outside"] is True
        assert out["gated"] is False
        assert out["reason"] == "insufficient_n"
        assert len(small) < MIN_BLOCKS

    def test_missing_current_value(self):
        payload = build_band_payload(TIGHT, seed_material="s")
        out = gate(TIGHT, None, payload)
        assert out == {"z": None, "outside": None, "confidence": None,
                       "gated": False, "reason": "no_current_value"}

    def test_missing_payload_is_no_band(self):
        out = gate(TIGHT, 200.0, None)
        assert out["gated"] is False and out["reason"] == "no_band"

    def test_garbage_payload_does_not_confirm(self):
        out = gate(TIGHT, 200.0, {"band": "not-a-dict", "k": "x"})
        assert out["gated"] is False and out["reason"] == "no_band"

    def test_bad_k_falls_back_to_the_default_threshold(self):
        payload = build_band_payload(TIGHT, seed_material="s")
        payload["k"] = "nope"
        out = gate(TIGHT, 200.0, payload)
        assert out["gated"] is True                # k 坏 → 用默认 3.5,不误判


class TestBandLine:
    def test_empty_without_confirmed_groups(self):
        assert band_line(None) == ""
        assert band_line({}) == ""
        assert band_line({"by_dim": {}}) == ""
        payload = build_band_payload(TIGHT, seed_material="s")
        within = {**payload, **gate(TIGHT, 105.0, payload)}
        assert band_line({"by_dim": {"华北": within}}) == ""

    def test_lists_confirmed_groups_with_z_and_k(self):
        payload = build_band_payload(TIGHT, seed_material="s")
        gated = {**payload, **gate(TIGHT, 200.0, payload)}
        line = band_line({"by_dim": {"华东": gated}})
        assert line.startswith("噪声带：")
        assert "华东" in line and "|z|=" in line and "> k=3.50" in line
        assert "位置" in line

    def test_sorted_by_abs_z_desc_and_capped_at_two(self):
        payload = build_band_payload(TIGHT, seed_material="s")
        strong = {**payload, **gate(TIGHT, 300.0, payload)}
        medium = {**payload, **gate(TIGHT, 200.0, payload)}
        weak = {**payload, **gate(TIGHT, 160.0, payload)}
        line = band_line({"by_dim": {"A": weak, "B": strong, "C": medium}})
        assert line.count("|z|=") == 2
        assert "B" in line and "C" in line and "A" not in line
