"""判定质量回评单测(纯):按 (rule_id, rule_rev) 分桶 + 比率只在分母够时给。

验收清单 7 的核心断言在这里:改 B 规则**不清 A 桶**(N2 —— 旧的
``rule_digest`` 对整份文件敏感,正是因此才有 rev);老 verdict 无 rev
落 ``rev_unknown`` 而不是猜成当前版本。其余纪律:
  - 比率是「判得出来」的那部分的分母(effective + no_effect),
    ``unverifiable`` 不进;
  - ``insufficient`` 明说原因(样本不足比一个 1/1 = 100% 诚实);
  - 有测量无 verdict(历史被清)的桶照常出现 —— total=0 自己说出来。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trove.services.decision import score as sc


def _v(rule_id="r1", rev="rev1", status="ok", triggered=False,
       at="2026-10-01T00:00:00"):
    return SimpleNamespace(rule_id=rule_id, status=status, triggered=triggered,
                           evaluated_at=at, evidence={"rule_rev": rev})


def _e(rule_id="r1", rev="rev1", band=True, error="", at="2026-10-10"):
    return {"rule_id": rule_id, "rule_rev": rev, "outside_band": band,
            "error": error, "measured_at": at}


class TestBucketKey:
    def test_shape(self):
        assert sc.bucket_key("r1", "abc123") == "r1@abc123"

    @pytest.mark.parametrize("rev", ["", None, "  "])
    def test_missing_rev_folds_to_unknown(self, rev):
        assert sc.bucket_key("r1", rev) == "r1@rev_unknown"


class TestRevOf:
    def test_reads_evidence_dict(self):
        assert sc.rev_of(_v(rev="xyz")) == "xyz"

    def test_attribute_form(self):
        obj = SimpleNamespace(evidence=None, rule_rev="zh")
        assert sc.rev_of(obj) == "zh"

    @pytest.mark.parametrize("bad", [SimpleNamespace(), {"evidence": "坏"},
                                     SimpleNamespace(evidence={"rule_rev": None})])
    def test_garbage_is_empty_not_a_guess(self, bad):
        assert sc.rev_of(bad) == ""


class TestScoreHistory:
    def test_empty(self):
        assert sc.score_history([]) == []
        assert sc.score_history(None) == []

    def test_counts_and_touch(self):
        buckets = sc.score_history([
            _v(status="ok", at="2026-10-01T00:00:00"),
            _v(status="alert", triggered=True, at="2026-10-03T00:00:00"),
        ])
        assert len(buckets) == 1
        b = buckets[0]
        assert (b["total"], b["ok"], b["alert"], b["triggered"]) == (2, 1, 1, 1)
        assert b["triggered_rate"] == 0.5
        assert (b["first_at"], b["last_at"]) == (
            "2026-10-01T00:00:00", "2026-10-03T00:00:00")
        assert "few_verdicts" in b["insufficient"]      # 2 < LOW_N

    def test_status_error_and_unknown_status(self):
        b = sc.score_history([
            _v(status="error"), _v(status="weird"), _v(status="ok"),
        ])[0]
        assert b["total"] == 3
        assert (b["ok"], b["alert"], b["error"]) == (1, 0, 1)

    def test_editing_rule_b_does_not_clear_rule_a_bucket(self):
        """N2 验收:改 B 开 B 的新桶,A 桶逐字不变。"""
        before = sc.score_history([_v("A", "a1"), _v("A", "a1"),
                                   _v("B", "b1")])
        after = sc.score_history([_v("A", "a1"), _v("A", "a1"),
                                  _v("B", "b1"), _v("B", "b2")])
        a_before = next(b for b in before if b["key"] == "A@a1")
        a_after = next(b for b in after if b["key"] == "A@a1")
        assert a_after == a_before
        assert {b["key"] for b in after} == {"A@a1", "B@b1", "B@b2"}

    def test_verdicts_without_evidence_land_in_rev_unknown(self):
        buckets = sc.score_history([_v(rev="")])
        assert buckets[0]["key"] == "r1@rev_unknown"

    def test_sorted_by_key(self):
        buckets = sc.score_history([_v("b", "x"), _v("a", "y"), _v("a", "x")])
        assert [b["key"] for b in buckets] == ["a@x", "a@y", "b@x"]


class TestEffects:
    def test_four_flags(self):
        buckets = sc.score_history([], effects=[
            _e(band=True), _e(band=False), _e(band=None), _e(error="boom")])
        b = buckets[0]
        assert b["effects"] == {"measured": 3, "effective": 1, "no_effect": 1,
                                "unverifiable": 1, "errors": 1}
        assert b["decided"] == 2

    def test_zero_is_no_effect_not_missing(self):
        """存储层的 0/1 与 Python 的 False/True 都要落位(0 不是"没测到")。"""
        b = sc.score_history([], effects=[_e(band=0), _e(band=1)])[0]
        assert b["effects"]["no_effect"] == 1
        assert b["effects"]["effective"] == 1
        assert b["effects"]["unverifiable"] == 0

    def test_rate_only_when_denominator_is_enough(self):
        two = sc.score_history([], effects=[_e(band=True), _e(band=True)])[0]
        assert two["effective_rate"] is None
        assert "few_effects" in two["insufficient"]

        three = sc.score_history([], effects=[
            _e(band=True), _e(band=True), _e(band=False)])[0]
        assert three["effective_rate"] == pytest.approx(2 / 3)
        assert "few_effects" not in three["insufficient"]

    def test_min_effects_override(self):
        one = sc.score_history([], effects=[_e(band=True)], min_effects=1)[0]
        assert one["effective_rate"] == 1.0

    def test_no_effects_at_all(self):
        b = sc.score_history([_v()])[0]
        assert "no_effects" in b["insufficient"]

    def test_effect_bucket_without_verdicts_appears(self):
        """历史被保留期清掉了 —— 效果是真的,桶照常出场(total=0 说出来)。"""
        buckets = sc.score_history([], effects=[_e("gone", "g1", True)] * 3)
        b = next(b for b in buckets if b["rule_id"] == "gone")
        assert b["total"] == 0
        assert b["effective_rate"] == 1.0
        assert b["insufficient"] == []                 # total=0 不算 few_verdicts

    def test_anchor_date_preferred_for_touch(self):
        e = {**_e(), "anchor_date": "2026-09-30", "measured_at": "2026-10-10"}
        b = sc.score_history([], effects=[e])[0]
        assert b["first_at"] == "2026-09-30"

    def test_non_dict_effects_skipped(self):
        buckets = sc.score_history([], effects=["x", None, 3])
        assert buckets == []

    def test_bucket_without_ok_status_has_null_rate(self):
        assert sc.score_history([]) == []
        b = sc.score_history([_v(status="error")])[0]
        assert b["triggered_rate"] == 0.0


class TestRollup:
    def test_sums_and_recomputes_rates(self):
        """总比率是重算的(总有效/总判出),不是各桶比率的平均 ——
        桶 A 1/1(=1.0)与桶 B 2/4(=0.5)平均是 0.75,重算是 3/5。"""
        buckets = sc.score_history([], effects=[
            _e("a", "x", True),
            _e("b", "y", True), _e("b", "y", True),
            _e("b", "y", False), _e("b", "y", False)])
        out = sc.rollup(buckets)
        assert out["buckets"] == 2
        assert out["decided"] == 5
        assert out["effective_rate"] == pytest.approx(3 / 5)
        assert out["insufficient"] == []

    def test_empty(self):
        out = sc.rollup([])
        assert out["buckets"] == 0 and out["total"] == 0
        assert out["triggered_rate"] is None
        assert out["effective_rate"] is None
        assert out["insufficient"] == ["no_effects"]

    def test_summary_obeys_the_same_denominator_gate(self):
        """一条测量的总览不是 100% —— 总览行不该比桶更敢说话(B8 收口)。"""
        out = sc.rollup(sc.score_history([], effects=[_e("a", "x", True)]))
        assert out["decided"] == 1 and out["effective_rate"] is None
        assert out["insufficient"] == ["few_effects"]
        # 门槛可调,判据同桶(min_effects 是同一个域参数)
        assert sc.rollup(
            sc.score_history([], effects=[_e("a", "x", True)]),
            min_effects=1)["effective_rate"] == 1.0

    def test_accepts_a_generator_once(self):
        """生成器只被消费一次(rollup 先 list 化 —— 曾经的 bug 钉)。"""
        gen = (b for b in sc.score_history([_v(), _v()]))
        out = sc.rollup(gen)
        assert out["buckets"] == 1 and out["total"] == 2

    def test_none_is_empty(self):
        assert sc.rollup(None)["buckets"] == 0
