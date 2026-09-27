"""Deterministic period math — 环比/同比 window derivation.

Cases moved here from ``tests/workflow/test_attribution.py`` when the
functions were extracted to ``trove.core.periods`` (attribution keeps a
``_base_period`` alias, so both import paths must stay green).
"""

from datetime import date, timedelta

from trove.core.periods import base_period, shift_months
from trove.workflow.nodes.parse_date import format_time_range, parse_time_range


class TestBasePeriod:
    def test_prev_period_shifts_equal_window(self):
        assert base_period("2024-01-01 ~ 2024-01-31", "prev_period") == (
            ("2024-01-01", "2024-01-31"),
            ("2023-12-01", "2023-12-31"),
        )

    def test_prev_period_single_day(self):
        assert base_period("2024-03-15 ~ 2024-03-15", "prev_period") == (
            ("2024-03-15", "2024-03-15"),
            ("2024-03-14", "2024-03-14"),
        )

    def test_yoy_shifts_one_year(self):
        assert base_period("2024-03-01 ~ 2024-03-31", "yoy") == (
            ("2024-03-01", "2024-03-31"),
            ("2023-03-01", "2023-03-31"),
        )

    def test_share_has_no_base(self):
        """`share` means "no baseline" — distinct from an unparseable window,
        which callers must not confuse it with (the decision layer never
        passes `share`, so a None there is always a real failure)."""
        assert base_period("2024-01-01 ~ 2024-01-31", "share") is None

    def test_invalid_format_returns_none(self):
        assert base_period("", "prev_period") is None
        assert base_period("not a range", "prev_period") is None
        assert base_period("2024-13-99 ~ x", "prev_period") is None


class TestShiftMonths:
    def test_plain_shift(self):
        assert shift_months(date(2024, 3, 15), -1) == date(2024, 2, 15)
        assert shift_months(date(2024, 3, 15), 12) == date(2025, 3, 15)

    def test_month_end_is_clamped_not_overflowed(self):
        """3/31 minus one month is 2/28 — the same convention parse_date uses."""
        assert shift_months(date(2024, 3, 31), -1) == date(2024, 2, 29)  # leap
        assert shift_months(date(2023, 3, 31), -1) == date(2023, 2, 28)
        assert shift_months(date(2024, 5, 31), -3) == date(2024, 2, 29)

    def test_crosses_year_boundary(self):
        assert shift_months(date(2024, 1, 15), -1) == date(2023, 12, 15)
        assert shift_months(date(2024, 12, 15), 1) == date(2025, 1, 15)


class TestIntegrationWithParseDate:
    """The decision layer composes parse_time_range → format_time_range →
    base_period. That seam is the whole reason the extraction was cheap, so
    pin it: a rule can declare its window as natural language and still get
    a real baseline window out the other end."""

    def test_natural_language_window_yields_a_baseline(self):
        rng = parse_time_range("本月", reference_date=date(2026, 9, 27), lang="zh")
        assert rng == (date(2026, 9, 1), date(2026, 9, 30))
        ctx = format_time_range(*rng)
        assert ctx == "2026-09-01 ~ 2026-09-30"
        # prev_period is an equal-*length* window, not the previous calendar
        # month: 30 days before Sep 1 is Aug 2–31, not Aug 1–31. Decision
        # rules inherit this definition of 环比.
        assert base_period(ctx, "prev_period") == (
            ("2026-09-01", "2026-09-30"),
            ("2026-08-02", "2026-08-31"),
        )
        assert base_period(ctx, "yoy") == (
            ("2026-09-01", "2026-09-30"),
            ("2025-09-01", "2025-09-30"),
        )

    def test_rolling_window_yields_an_equal_length_baseline(self):
        rng = parse_time_range("最近7天", reference_date=date(2026, 9, 27), lang="zh")
        assert rng is not None
        cur, base = base_period(format_time_range(*rng), "prev_period")
        span = (date.fromisoformat(cur[1]) - date.fromisoformat(cur[0])).days
        assert (date.fromisoformat(base[1]) - date.fromisoformat(base[0])).days == span
        assert date.fromisoformat(base[1]) == date.fromisoformat(cur[0]) - timedelta(days=1)

    def test_unmatched_window_is_none_not_an_error(self):
        """parse_time_range is lenient by design (callers pass through). The
        decision layer is the one that must turn this None into a hard error."""
        assert parse_time_range("随便说点什么", reference_date=date(2026, 9, 27)) is None
