"""Cron math tests — pure, deterministic."""

from __future__ import annotations

from datetime import datetime

from trove.services.jobs.cron import cron_next, interval_next, parse_cron


class TestCron:
    def test_star_every_minute(self):
        nxt = cron_next("* * * * *", datetime(2026, 8, 20, 10, 0, 0))
        assert nxt == datetime(2026, 8, 20, 10, 1, 0)

    def test_step_minutes(self):
        nxt = cron_next("*/15 * * * *", datetime(2026, 8, 20, 10, 3))
        assert nxt == datetime(2026, 8, 20, 10, 15)
        nxt = cron_next("*/15 * * * *", datetime(2026, 8, 20, 10, 45))
        assert nxt == datetime(2026, 8, 20, 11, 0)

    def test_daily_at_nine_rolls_next_day(self):
        nxt = cron_next("0 9 * * *", datetime(2026, 8, 20, 10, 0))
        assert nxt == datetime(2026, 8, 21, 9, 0)

    def test_weekly_dow(self):
        # 2026-08-19 is a Wednesday
        nxt = cron_next("0 9 * * 3", datetime(2026, 8, 18, 0, 0))
        assert nxt == datetime(2026, 8, 19, 9, 0)

    def test_sunday_alias_7_and_0(self):
        assert cron_next("0 9 * * 7", datetime(2026, 8, 20)) == cron_next(
            "0 9 * * 0", datetime(2026, 8, 20)
        )

    def test_range_and_list(self):
        assert cron_next("0 9-11 * * *", datetime(2026, 8, 20, 8, 30)) == datetime(2026, 8, 20, 9, 0)
        assert cron_next("0,30 * * * *", datetime(2026, 8, 20, 10, 10)) == datetime(2026, 8, 20, 10, 30)

    def test_dom_restricted_alone_matches_dom_only(self):
        # dom-only Feb 30 never exists → None
        assert cron_next("0 0 30 2 *", datetime(2026, 1, 1)) is None
        assert cron_next("0 0 30 * *", datetime(2026, 1, 1)) == datetime(2026, 1, 30)

    def test_both_restricted_uses_or_semantics(self):
        # dom=1st OR Monday
        nxt = cron_next("0 9 1 * 1", datetime(2026, 8, 2))
        # 2026-08-03 is a Monday
        assert nxt == datetime(2026, 8, 3, 9, 0)

    def test_invalid_expressions_none(self):
        assert cron_next("99 99 99 99 99", datetime(2026, 1, 1)) is None
        assert cron_next("not cron", datetime(2026, 1, 1)) is None
        assert cron_next("", datetime(2026, 1, 1)) is None
        assert parse_cron("0 9 * *") is None

    def test_interval(self):
        assert interval_next(30, datetime(2026, 8, 20, 10, 0)) == datetime(2026, 8, 20, 10, 30)
        assert interval_next(0, datetime(2026, 8, 20, 10, 0)) is None