"""Token calibration (estimate vs actual EMA) tests."""

import pytest

from trove.llm import token_calibration
from trove.llm.token_calibration import factor, record, reset


@pytest.fixture(autouse=True)
def _clean():
    reset()
    yield
    reset()


class TestCalibration:
    def test_cold_start_factor_is_one(self):
        assert factor("gpt-4o", "sqlite") == 1.0

    def test_record_and_factor_scale_estimates(self):
        record("gpt-4o", "sqlite", estimated=100, actual=130)
        assert factor("gpt-4o", "sqlite") > 1.0  # 实测高于估算 → 放大成本

    def test_under_estimate_factors_below_one(self):
        record("gpt-4o", "sqlite", estimated=100, actual=80)
        assert factor("gpt-4o", "sqlite") < 1.0

    def test_ema_smooths_across_observations(self):
        record("gpt-4o", "sqlite", 100, 200)   # ratio 2.0
        record("gpt-4o", "sqlite", 100, 200)   # 再次同 ratio → 收敛到 ~2.0
        assert factor("gpt-4o", "sqlite") == pytest.approx(2.0, abs=0.01)

    def test_per_model_dialect_isolation(self):
        record("gpt-4o", "sqlite", 100, 200)
        assert factor("gpt-4o", "mysql") == 1.0
        assert factor("claude", "sqlite") == 1.0

    def test_non_positive_signals_ignored(self):
        record("gpt-4o", "sqlite", 0, 100)
        record("gpt-4o", "sqlite", 100, 0)
        assert factor("gpt-4o", "sqlite") == 1.0

    def test_reset_clears(self):
        record("gpt-4o", "sqlite", 100, 130)
        reset()
        assert factor("gpt-4o", "sqlite") == 1.0
