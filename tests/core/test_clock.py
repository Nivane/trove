"""落盘时间戳的唯一时钟（P3）：写侧 aware UTC，读侧容忍旧 naive 本地。

这些测试**不依赖运行机器时区**：凡是 naive → 瞬时的解释，都显式传
``legacy_tz``，默认时区那条只断言「等同于用本地时区解释」（机器本身的
``astimezone`` 就是答案，不预设某个偏移）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trove.core.clock import (
    cutoff_pair,
    era_predicate,
    iso_utc,
    parse_utc,
    utc_now,
    utc_now_iso,
)

#: 一个固定、与 UTC 有偏移、且不随夏令时变化的旧纪元时区。
LEGACY = timezone(timedelta(hours=-7))


class TestUtcNow:
    def test_is_aware_utc(self):
        now = utc_now()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(0)

    def test_iso_form_carries_the_offset_suffix(self):
        text = utc_now_iso()
        assert text.endswith("+00:00")
        # seconds precision: no fractional part sneaks in
        assert "." not in text


class TestIsoUtc:
    def test_normalizes_other_offsets_to_utc(self):
        local = datetime(2026, 10, 26, 12, 0, 0, tzinfo=LEGACY)
        assert iso_utc(local) == "2026-10-26T19:00:00+00:00"

    def test_rejects_naive(self):
        with pytest.raises(ValueError, match="aware"):
            iso_utc(datetime(2026, 10, 26, 12, 0, 0))

    def test_timespec(self):
        aware = datetime(2026, 10, 26, 12, 0, 0, 123456, tzinfo=timezone.utc)
        assert iso_utc(aware, timespec="milliseconds") == (
            "2026-10-26T12:00:00.123+00:00")


class TestParseUtc:
    def test_aware_string_is_normalized_to_utc(self):
        parsed = parse_utc("2026-10-26T12:00:00-07:00")
        assert parsed == datetime(2026, 10, 26, 19, 0, tzinfo=timezone.utc)

    def test_z_suffix_is_accepted(self):
        parsed = parse_utc("2026-10-26T19:00:00Z")
        assert parsed == datetime(2026, 10, 26, 19, 0, tzinfo=timezone.utc)

    def test_naive_string_is_the_legacy_local_era(self):
        parsed = parse_utc("2026-10-26T12:00:00", legacy_tz=LEGACY)
        assert parsed == datetime(2026, 10, 26, 19, 0, tzinfo=timezone.utc)

    def test_naive_string_defaults_to_the_machine_local_zone(self):
        raw = "2026-10-26T12:00:00"
        expected = datetime.fromisoformat(raw).astimezone()
        assert parse_utc(raw) == expected.astimezone(timezone.utc)

    def test_aware_datetime_passthrough(self):
        value = datetime(2026, 10, 26, 12, 0, tzinfo=LEGACY)
        assert parse_utc(value) == datetime(2026, 10, 26, 19, 0,
                                            tzinfo=timezone.utc)

    def test_naive_datetime_uses_the_legacy_zone(self):
        value = datetime(2026, 10, 26, 12, 0, 0)
        assert parse_utc(value, legacy_tz=LEGACY) == datetime(
            2026, 10, 26, 19, 0, tzinfo=timezone.utc)

    @pytest.mark.parametrize("value", ["", "   ", "not-a-time", None, 42, []])
    def test_unreadable_is_none_never_now(self, value):
        assert parse_utc(value) is None


class TestCutoffPair:
    def test_both_forms_are_the_same_instant(self):
        aware, legacy = cutoff_pair("2026-10-26T19:00:00+00:00",
                                    legacy_tz=LEGACY)
        assert aware == "2026-10-26T19:00:00+00:00"
        assert legacy == "2026-10-26T12:00:00"
        assert parse_utc(aware) == parse_utc(legacy, legacy_tz=LEGACY)

    def test_unparseable_returns_none(self):
        assert cutoff_pair("nonsense") is None


class TestEraPredicate:
    def test_shape_covers_both_eras(self):
        built = era_predicate("expires_at", "<", "2026-10-26T19:00:00Z",
                              legacy_tz=LEGACY)
        assert built is not None
        sql, params = built
        assert sql == (
            "((expires_at LIKE '%+00:00' AND expires_at < ?)"
            " OR (expires_at NOT LIKE '%+00:00' AND expires_at < ?))")
        assert params == ("2026-10-26T19:00:00+00:00", "2026-10-26T12:00:00")

    def test_supports_at_or_after(self):
        built = era_predicate("attempted_at", ">=", "2026-10-26T19:00:00Z",
                              legacy_tz=LEGACY)
        assert built is not None
        assert "attempted_at >= ?" in built[0]

    def test_unknown_operator_is_a_programming_error(self):
        with pytest.raises(ValueError, match="operator"):
            era_predicate("expires_at", "=", "2026-10-26T19:00:00Z")

    def test_unparseable_cutoff_returns_none(self):
        assert era_predicate("expires_at", "<", "nonsense") is None
