"""``scan.models`` —— 全闭集形状。

这个文件的重点是**拒绝面**:mode/kind/status/direction/min_pct 任一
落到集合外都必须是解析错误(响亮),因为它们最终会变成草稿、通知与
治理待办 —— 一个被静默取默认的枚举值,最后会变成一条"看起来在跑、
其实永远不触发"的规则。
"""

from __future__ import annotations

import pytest

from trove.services.scan.models import (
    DIRECTIONS,
    FINDING_KINDS,
    HYPOTHESIS_STATUSES,
    SCAN_MODES,
    Finding,
    Hypothesis,
    ScanError,
    ScanSpec,
    VerifiedHypothesis,
    slug,
)


class TestScanSpec:
    def test_minimal_spec_defaults(self):
        spec = ScanSpec.from_dict({"metrics": ["loan_balance"]})
        assert spec.metrics == ("loan_balance",)
        assert spec.dimensions == ()
        assert spec.mode == "trailing"
        assert spec.lookback == 12
        assert spec.k == 3.5
        assert spec.top_k == 5
        assert spec.hypotheses is False      # 定时扫描默认关(成本面)

    def test_names_are_stripped_and_empties_dropped(self):
        spec = ScanSpec.from_dict({
            "metrics": [" loan_balance ", ""],
            "dimensions": [" region ", None],
        })
        assert spec.metrics == ("loan_balance",)
        assert spec.dimensions == ("region",)

    def test_units_are_metric_by_dim_with_aggregate_view(self):
        spec = ScanSpec.from_dict({"metrics": ["m1", "m2"], "dimensions": ["d"]})
        assert spec.units == [("m1", "d"), ("m2", "d")]

    def test_units_without_dimensions_scan_the_whole_metric(self):
        spec = ScanSpec.from_dict({"metrics": ["m1"]})
        assert spec.units == [("m1", "")]

    def test_roundtrip_dict(self):
        spec = ScanSpec.from_dict({
            "metrics": ["m"], "dimensions": ["d"], "window": "本月",
            "grain": "month", "mode": "same_phase", "lookback": 4,
            "k": 2.5, "top_k": 2, "hypotheses": True,
        })
        again = ScanSpec.from_dict(spec.to_dict())
        assert again == spec

    @pytest.mark.parametrize("raw", [None, [], "metrics=loan_balance", 3])
    def test_non_mapping_is_rejected(self, raw):
        with pytest.raises(ScanError):
            ScanSpec.from_dict(raw)

    def test_metrics_are_required(self):
        with pytest.raises(ScanError, match="at least one metric"):
            ScanSpec.from_dict({"metrics": []})
        with pytest.raises(ScanError, match="must be a list"):
            ScanSpec.from_dict({"metrics": "loan_balance"})

    def test_bad_mode_is_rejected_not_defaulted(self):
        with pytest.raises(ScanError, match="mode"):
            ScanSpec.from_dict({"metrics": ["m"], "mode": "seasonal"})

    def test_bad_grain_is_rejected(self):
        with pytest.raises(ScanError, match="grain"):
            ScanSpec.from_dict({"metrics": ["m"], "grain": "quarter"})

    def test_empty_grain_means_derive_from_window(self):
        assert ScanSpec.from_dict({"metrics": ["m"], "grain": ""}).grain == ""

    def test_numeric_guards(self):
        with pytest.raises(ScanError, match="non-numeric"):
            ScanSpec.from_dict({"metrics": ["m"], "lookback": "十几"})
        with pytest.raises(ScanError, match="lookback"):
            ScanSpec.from_dict({"metrics": ["m"], "lookback": 0})
        with pytest.raises(ScanError, match="top_k"):
            ScanSpec.from_dict({"metrics": ["m"], "top_k": 0})
        with pytest.raises(ScanError, match="k must be"):
            ScanSpec.from_dict({"metrics": ["m"], "k": 0})
        with pytest.raises(ScanError, match="k must be"):
            ScanSpec.from_dict({"metrics": ["m"], "k": float("nan")})

    def test_mode_vocabulary_is_closed(self):
        assert SCAN_MODES == ("trailing", "same_phase")
        assert FINDING_KINDS == ("anomaly", "unverifiable")
        assert HYPOTHESIS_STATUSES == ("supported", "refuted", "unverifiable", "no_data")
        assert DIRECTIONS == ("up", "down")


class TestHypothesis:
    def test_minimal(self):
        h = Hypothesis.from_dict({"claim": "c", "metric": "m", "direction": "up"})
        assert (h.direction, h.min_pct, h.dimension, h.value) == ("up", 0.0, "", "")

    def test_roundtrip(self):
        h = Hypothesis.from_dict({
            "claim": "c", "metric": "m", "dimension": "region",
            "value": "华东", "direction": "down", "min_pct": 0.25,
        })
        assert Hypothesis.from_dict(h.to_dict()) == h

    def test_claim_and_metric_are_required(self):
        with pytest.raises(ScanError, match="claim"):
            Hypothesis.from_dict({"claim": " ", "metric": "m"})
        with pytest.raises(ScanError, match="metric"):
            Hypothesis.from_dict({"claim": "c", "metric": ""})

    def test_direction_is_closed(self):
        with pytest.raises(ScanError, match="direction"):
            Hypothesis.from_dict({"claim": "c", "metric": "m", "direction": "sideways"})
        # 缺 direction 同样拒 —— 方向是假设的一半,没有"默认朝上"这种假设。
        with pytest.raises(ScanError, match="direction"):
            Hypothesis.from_dict({"claim": "c", "metric": "m"})

    def test_min_pct_must_be_a_fraction(self):
        base = {"claim": "c", "metric": "m", "direction": "up"}
        with pytest.raises(ScanError, match="min_pct"):
            Hypothesis.from_dict({**base, "min_pct": 1.5})
        with pytest.raises(ScanError, match="min_pct"):
            Hypothesis.from_dict({**base, "min_pct": -0.1})
        with pytest.raises(ScanError, match="min_pct"):
            Hypothesis.from_dict({**base, "min_pct": "多"})

    def test_fields_are_bounded(self):
        h = Hypothesis.from_dict({
            "claim": "字" * 500, "metric": "m", "direction": "up",
            "value": "v" * 500,
        })
        assert len(h.claim) == 400 and len(h.value) == 200


class TestVerifiedHypothesis:
    def test_one_row_shape(self):
        v = VerifiedHypothesis(hypothesis=Hypothesis(claim="c", metric="m"))
        d = v.to_dict()
        assert set(d) == {"hypothesis", "status", "observed", "reason", "queries"}
        assert d["status"] == "unverifiable"    # 默认不是 supported


class TestFinding:
    def test_label(self):
        f = Finding(metric="m", dimension="region", value="华东")
        assert f.label == "m / region=华东"
        assert Finding(metric="m").label == "m"


class TestSlug:
    def test_deterministic(self):
        assert slug("loan_balance") == "loan_balance"
        assert slug("Loan Balance") == "loan-balance"
        assert slug("loan.balance") == "loan-balance"

    def test_cjk_survives(self):
        """汉字是词字符 —— 折成空串会让两个地区值撞成同一个草稿 id。"""
        assert slug("华东") == "华东"
        assert slug("华东") != slug("华北")

    def test_empty_and_bounded(self):
        assert slug("") == "x"
        assert slug("!!!") == "x"
        assert len(slug("a" * 200)) <= 48
