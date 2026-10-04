"""统计器械单测:表驱动 + 「算不出 → None」纪律 + 播种确定性。

三条纪律的测试形态:
  - 每个函数一组「算不出 → None + reason」用例(MAD=0 / 空 / n<3);
  - 播种用 sha256(跨进程稳定)—— 用 PYTHONHASHSEED 三连子进程直接钉;
  - 结论带 method / seed_material / n(可复算的元数据)。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from trove.services.analysis import stats as st

#: 仓根(子进程复用工作树导入路径,editable 安装下即本树)
ROOT = Path(__file__).resolve().parents[3]


class TestRobustCore:
    def test_median_mad_quantile_basic(self):
        xs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        assert st.median(xs) == 5.5
        # MAD(原始)= median(|x − 5.5|) = 2.5;× 1.4826 正态一致化
        assert st.mad(xs) == pytest.approx(2.5 * 1.4826)
        assert st.quantile(xs, 0.5) == 5.5
        assert st.quantile(xs, 0.0) == 1.0
        assert st.quantile(xs, 1.0) == 10.0

    def test_type7_interpolation(self):
        # h = (4−1)·0.25 = 0.75 → 1 + 0.75·(2−1) = 1.75(Hyndman-Fan type-7)
        assert st.quantile([1, 2, 3, 4], 0.25) == pytest.approx(1.75)

    def test_mean(self):
        assert st.mean([1, 2, 3]) == 2.0
        assert st.mean([]) is None

    def test_robust_z_value_and_scale_consistency(self):
        xs = [10, 11, 12, 11, 10, 11, 12, 11, 10, 11]
        med, scale = st.median(xs), st.mad(xs)
        assert st.robust_z(13.0, xs) == pytest.approx((13.0 - med) / scale)

    def test_zero_mad_returns_none(self):
        """计划钉死用例:全同 + 一个离群 → MAD=0。

        0.0 是**算得出**的散布值,但 z 判不了 —— 必须 None,
        绝不返回 inf 或自信的假 0。
        """
        xs = [10, 10, 10, 10, 50]
        assert st.mad(xs) == 0.0
        assert st.robust_z(50, xs) is None

    def test_non_finite_and_none_filtered(self):
        assert st.median([None, float("nan"), float("inf"), 2]) == 2.0
        assert st.median([]) is None
        assert st.median([None]) is None
        assert st.mad([]) is None
        assert st.quantile([], 0.5) is None
        assert st.robust_z(1.0, []) is None

    def test_quantile_clamps_q(self):
        assert st.quantile([1, 2, 3], -1.0) == 1.0
        assert st.quantile([1, 2, 3], 2.0) == 3.0


class TestBand:
    def test_band_center_scale_bounds(self):
        xs = [10, 11, 12, 11, 10, 11, 12, 11, 10, 11]
        b = st.band(xs)
        half = 3.5 * b.scale
        assert b.center == pytest.approx(11.0)
        assert b.lo == pytest.approx(b.center - half)
        assert b.hi == pytest.approx(b.center + half)
        assert b.method == "robust" and b.n == 10
        # outside 与 |robust_z| > k 由构造同义
        assert st.outside(15.0, b) is (abs(st.robust_z(15.0, xs)) > 3.5)
        assert st.outside(11.0, b) is False

    def test_band_insufficient_n_still_gives_bounds(self):
        b = st.band([1.0, 2.0, 3.0])
        assert "insufficient_n" in b.degraded      # 3 < MIN_BLOCKS
        assert b.lo is not None and b.n == 3       # 带仍给,标不可省

    def test_band_zero_scale_unusable(self):
        b = st.band([5.0] * 9)
        assert "zero_scale" in b.degraded
        assert b.lo is None and b.hi is None and b.center == 5.0
        assert st.outside(9.0, b) is None          # 判不了,不是 False

    def test_band_no_data(self):
        b = st.band([])
        assert "no_data" in b.degraded and b.lo is None
        assert st.outside(1.0, b) is None

    def test_to_dict_roundtrip(self):
        b = st.band([1.0, 2.0, 3.0])
        d = b.to_dict()
        assert d["center"] == b.center and d["degraded"] == list(b.degraded)


class TestBootstrap:
    def test_seed_material_determinism(self):
        xs = [10, 12, 11, 9, 13, 11, 10, 12, 30, 11]
        a = st.bootstrap_ci(xs, iters=400, seed_material="demo|net|day|2024")
        b = st.bootstrap_ci(xs, iters=400, seed_material="demo|net|day|2024")
        assert a == b                              # 同材料 → 逐位相同
        assert a["lo"] <= a["hi"]
        assert a["method"] == "bootstrap_percentile" and a["n"] == len(xs)
        assert a["seed_material"] == "demo|net|day|2024"

    def test_insufficient(self):
        assert st.bootstrap_ci([1.0]) is None
        assert st.bootstrap_ci([]) is None

    def test_level_widens_interval(self):
        xs = [10, 12, 11, 9, 13, 11, 10, 12, 30, 11]
        narrow = st.bootstrap_ci(xs, level=0.5, iters=600, seed_material="m")
        wide = st.bootstrap_ci(xs, level=0.99, iters=600, seed_material="m")
        assert (wide["hi"] - wide["lo"]) >= (narrow["hi"] - narrow["lo"])


class TestEffectiveN:
    def test_positive_autocorrelation_shrinks_n(self):
        # 强正自相关(单调递增)→ ρ1 高 → n_eff < n
        xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        n_eff = st.effective_n(xs)
        assert n_eff is not None and 1.0 <= n_eff < len(xs)

    def test_alternating_negative_autocorr_caps_at_n(self):
        xs = [1.0, 10.0] * 6
        n_eff = st.effective_n(xs)
        assert n_eff is not None and n_eff <= float(len(xs))

    def test_undefined_cases_none(self):
        assert st.effective_n([1.0, 2.0]) is None          # n < 3
        assert st.effective_n([5.0] * 9) is None           # 常数序列无 ρ1


class TestWelch:
    def test_delta_sign_and_t(self):
        a = [10.0, 11.0, 12.0, 11.0, 10.0]
        b = [4.0, 5.0, 6.0, 5.0, 4.0]
        w = st.welch_delta(a, b)
        assert w is not None and w["delta"] == pytest.approx(6.0)
        assert w["t"] > 0 and w["method"] == "welch"
        assert w["n_a"] == 5 and w["n_b"] == 5
        assert "p_value" not in w and "p" not in w         # 不输出 p 值(伪精度)

    def test_zero_variance_none(self):
        assert st.welch_delta([1.0, 1.0], [2.0, 2.0]) is None
        assert st.welch_delta([1.0], [2.0, 3.0]) is None


class TestLowN:
    def test_flag(self):
        assert st.low_n(11) is True
        assert st.low_n(12) is False
        assert st.low_n([1.0, 2.0]) is True               # 按值列表计数
        assert st.low_n([1.0] * 12) is False


class TestCrossProcessDeterminism:
    """sha256 播种的直接证据:不同 PYTHONHASHSEED 子进程输出逐位一致。

    若哪天有人把 ``_seed`` 换成 ``hash()``(或依赖集合迭代序),
    这条用例会在三连子进程里翻红 —— 与 decompose 的保序修正是同源问题。
    """

    _CODE = (
        "from trove.services.analysis.stats import bootstrap_ci, effective_n;"
        "c = bootstrap_ci([10,12,11,9,13,11,10,12,30,11], iters=300,"
        " seed_material='demo|net|day');"
        "print(repr((c['lo'], c['hi'], effective_n([1,2,3,4,5,6,7,8,9,10]))))"
    )

    def test_bootstrap_stable_across_hash_seeds(self):
        outs = set()
        for seed in ("0", "1", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            r = subprocess.run(
                [sys.executable, "-c", self._CODE],
                capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120,
            )
            assert r.returncode == 0, r.stderr
            outs.add(r.stdout.strip())
        assert len(outs) == 1, f"跨进程不一致: {outs}"
