"""Parse-date node tests — deterministic time-expression resolution.

Vectors follow the Datus date_parser_zh/en templates (fixed reference
dates, duration vs period dichotomy, zh Monday / en Sunday week start).
"""

from datetime import date

import pytest

from trove.core.config import AgentConfig
from trove.workflow.nodes.parse_date import (
    format_time_range,
    make_parse_date,
    parse_time_range,
)
from trove.workflow.state import WorkflowState


def make_state(**kwargs) -> WorkflowState:
    defaults = {"session_id": "s1", "question": "Average grade by county"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


# ── zh vectors (Datus date_parser_zh examples) ────────────


@pytest.mark.parametrize(
    "question,ref,expected",
    [
        # 未来三个月内 / ref 2025-01-01 (duration)
        ("未来三个月内", date(2025, 1, 1), (date(2025, 1, 1), date(2025, 4, 1))),
        # 最近三个月 (duration)
        ("最近三个月", date(2025, 1, 1), (date(2024, 10, 1), date(2025, 1, 1))),
        # 下个月 / 上个月 (period, full calendar month)
        ("下个月", date(2025, 1, 15), (date(2025, 2, 1), date(2025, 2, 28))),
        ("上个月", date(2025, 1, 15), (date(2024, 12, 1), date(2024, 12, 31))),
        # 未来30天内 / 最近7天 (duration, days)
        ("未来30天内", date(2025, 1, 1), (date(2025, 1, 1), date(2025, 1, 31))),
        ("最近7天", date(2025, 1, 1), (date(2024, 12, 25), date(2025, 1, 1))),
        # 上周 / 下周 (period, zh week = Monday..Sunday)
        ("上周", date(2025, 2, 15), (date(2025, 2, 3), date(2025, 2, 9))),
        ("下周", date(2025, 2, 15), (date(2025, 2, 17), date(2025, 2, 23))),
        # 接下来两周 / 最近两周 (duration, weeks)
        ("接下来两周", date(2025, 2, 15), (date(2025, 2, 15), date(2025, 3, 1))),
        ("最近两周", date(2025, 2, 15), (date(2025, 2, 1), date(2025, 2, 15))),
        # 今年 (period, full year)
        ("今年", date(2025, 6, 15), (date(2025, 1, 1), date(2025, 12, 31))),
        # 从上个月到下个月 (composite)
        ("从上个月到下个月", date(2025, 1, 15), (date(2024, 12, 1), date(2025, 2, 28))),
        # 2024年底到现在 (half-absolute with ref tail)
        ("2024年底到现在", date(2025, 1, 15), (date(2024, 12, 31), date(2025, 1, 15))),
    ],
)
def test_parse_time_range_zh(question, ref, expected):
    assert parse_time_range(question, ref, lang="zh") == expected


# ── en vectors (Datus date_parser_en examples) ────────────


@pytest.mark.parametrize(
    "question,ref,expected",
    [
        ("next three months", date(2025, 1, 1), (date(2025, 1, 1), date(2025, 4, 1))),
        ("last three months", date(2025, 1, 1), (date(2024, 10, 1), date(2025, 1, 1))),
        ("next month", date(2025, 1, 15), (date(2025, 2, 1), date(2025, 2, 28))),
        ("last month", date(2025, 1, 15), (date(2024, 12, 1), date(2024, 12, 31))),
        ("next 30 days", date(2025, 1, 1), (date(2025, 1, 1), date(2025, 1, 31))),
        ("last 7 days", date(2025, 1, 1), (date(2024, 12, 25), date(2025, 1, 1))),
        # ref is a Wednesday; en week = Sunday..Saturday
        ("last week", date(2025, 1, 15), (date(2025, 1, 5), date(2025, 1, 11))),
        ("next week", date(2025, 1, 15), (date(2025, 1, 19), date(2025, 1, 25))),
        ("last two weeks", date(2025, 2, 15), (date(2025, 2, 1), date(2025, 2, 15))),
        ("this year", date(2025, 6, 15), (date(2025, 1, 1), date(2025, 12, 31))),
        ("from last month to next month", date(2025, 1, 15), (date(2024, 12, 1), date(2025, 2, 28))),
        ("from end of 2024 to now", date(2025, 1, 15), (date(2024, 12, 31), date(2025, 1, 15))),
    ],
)
def test_parse_time_range_en(question, ref, expected):
    assert parse_time_range(question, ref, lang="en") == expected


# ── Edge cases ────────────────────────────────────────────


class TestParseTimeRangeEdgeCases:
    def test_chinese_numerals(self):
        assert parse_time_range("最近三天", date(2025, 3, 10)) == (date(2025, 3, 7), date(2025, 3, 10))
        assert parse_time_range("最近十一天", date(2025, 3, 10)) == (date(2025, 2, 27), date(2025, 3, 10))
        assert parse_time_range("最近两个月", date(2025, 3, 10)) == (date(2025, 1, 10), date(2025, 3, 10))

    def test_month_end_clamping(self):
        assert parse_time_range("未来一个月", date(2025, 1, 31)) == (date(2025, 1, 31), date(2025, 2, 28))
        assert parse_time_range("最近一个月", date(2025, 1, 31)) == (date(2024, 12, 31), date(2025, 1, 31))

    def test_leap_year_february(self):
        assert parse_time_range("上个月", date(2024, 3, 31)) == (date(2024, 2, 1), date(2024, 2, 29))

    def test_week_across_year_boundary(self):
        assert parse_time_range("上周", date(2026, 1, 1)) == (date(2025, 12, 22), date(2025, 12, 28))

    def test_single_day_anchors(self):
        ref = date(2026, 8, 16)
        assert parse_time_range("今天", ref) == (ref, ref)
        assert parse_time_range("昨天", ref) == (date(2026, 8, 15), date(2026, 8, 15))
        assert parse_time_range("today", ref, lang="en") == (ref, ref)

    def test_year_duration(self):
        ref = date(2026, 8, 16)
        assert parse_time_range("最近三年", ref) == (date(2023, 8, 16), date(2026, 8, 16))

    def test_question_embedding(self):
        """Time expressions embedded in a full question still resolve."""
        assert parse_time_range(
            "最近7天每个学生的平均成绩是多少",
            date(2025, 1, 1),
        ) == (date(2024, 12, 25), date(2025, 1, 1))
        assert parse_time_range(
            "前7天的订单有多少",
            date(2025, 1, 10),
        ) == (date(2025, 1, 3), date(2025, 1, 9))


# ── Offsets (前N天 / N days ago) ───────────────────────────


class TestParseTimeRangeOffsets:
    def test_zh_prev_days_excludes_reference(self):
        assert parse_time_range("前7天", date(2026, 8, 16)) == (date(2026, 8, 9), date(2026, 8, 15))

    def test_zh_prev_weeks(self):
        assert parse_time_range("前两周", date(2026, 8, 16)) == (date(2026, 8, 2), date(2026, 8, 15))

    def test_zh_prev_month_clamped(self):
        assert parse_time_range("前一个月", date(2025, 3, 31)) == (date(2025, 2, 28), date(2025, 3, 30))

    def test_zh_ago_single_point(self):
        assert parse_time_range("7天前", date(2026, 8, 16)) == (date(2026, 8, 9), date(2026, 8, 9))
        assert parse_time_range("三个月前", date(2025, 1, 31)) == (date(2024, 10, 31), date(2024, 10, 31))

    def test_zh_hundreds_numeral(self):
        assert parse_time_range("前一百天", date(2026, 8, 16)) == (date(2026, 5, 8), date(2026, 8, 15))

    def test_en_ago_single_point(self):
        ref = date(2026, 8, 16)
        assert parse_time_range("7 days ago", ref, lang="en") == (date(2026, 8, 9), date(2026, 8, 9))
        assert parse_time_range("three weeks ago", ref, lang="en") == (date(2026, 7, 26), date(2026, 7, 26))

    def test_en_number_words_to_twenty(self):
        assert parse_time_range("thirteen days ago", date(2026, 8, 16), lang="en") == (
            date(2026, 8, 3), date(2026, 8, 3))


# ── Quarters (本季度 / this quarter) ───────────────────────


class TestParseTimeRangeQuarters:
    def test_zh_current_quarter(self):
        assert parse_time_range("本季度", date(2026, 8, 16)) == (date(2026, 7, 1), date(2026, 9, 30))

    def test_zh_previous_quarter(self):
        assert parse_time_range("上季度", date(2026, 8, 16)) == (date(2026, 4, 1), date(2026, 6, 30))

    def test_zh_next_quarter_wraps_year(self):
        assert parse_time_range("下季度", date(2026, 11, 16)) == (date(2027, 1, 1), date(2027, 3, 31))

    def test_en_quarter(self):
        assert parse_time_range("last quarter", date(2026, 8, 16), lang="en") == (
            date(2026, 4, 1), date(2026, 6, 30))
        assert parse_time_range("this quarter", date(2026, 8, 16), lang="en") == (
            date(2026, 7, 1), date(2026, 9, 30))


# ── Since (X以来/至今, since X) ────────────────────────────


class TestParseTimeRangeSince:
    def test_zh_since_period(self):
        assert parse_time_range("去年以来", date(2026, 8, 16)) == (date(2025, 1, 1), date(2026, 8, 16))

    def test_zh_since_absolute_year(self):
        assert parse_time_range("2024年以来", date(2026, 8, 16)) == (date(2024, 1, 1), date(2026, 8, 16))

    def test_zh_since_upto_now(self):
        assert parse_time_range("上个月至今", date(2026, 8, 16)) == (date(2026, 7, 1), date(2026, 8, 16))

    def test_en_since_period(self):
        assert parse_time_range("since last year", date(2026, 8, 16), lang="en") == (
            date(2025, 1, 1), date(2026, 8, 16))

    def test_en_since_absolute_year(self):
        assert parse_time_range("since 2024", date(2026, 8, 16), lang="en") == (
            date(2024, 1, 1), date(2026, 8, 16))

    def test_en_since_resolvable_partial_start(self):
        """since 起点部分可解析(day anchor 忽略后缀噪音)→ 半开区间。"""
        assert parse_time_range("since yesterday noon", date(2026, 8, 16), lang="en") == (
            date(2026, 8, 15), date(2026, 8, 16))

    def test_en_since_unresolvable_start_is_miss(self):
        assert parse_time_range("since the big celebration", date(2026, 8, 16), lang="en") is None


# ── Misses (must return None) ─────────────────────────────


class TestParseTimeRangeMiss:
    @pytest.mark.parametrize(
        "question,lang",
        [
            ("每个学生的平均成绩", "zh"),
            ("average grade by county", "en"),
            ("在附近的学生", "zh"),          # 防「近」误伤
            ("前天", "zh"),                  # 「前」+ 天 无数字 → 不误吞
            ("3月1日以来", "zh"),            # 半绝对 v1 外(无四位年份/月日不可解析)
            ("去年底到现在", "zh"),          # 半绝对 v1 外(无四位年份)
            ("上周", "en"),                  # 语言错配
            ("last week", "zh"),
        ],
    )
    def test_miss(self, question, lang):
        assert parse_time_range(question, date(2025, 1, 1), lang=lang) is None


# ── Formatting ────────────────────────────────────────────


def test_format_time_range():
    assert format_time_range(date(2025, 1, 1), date(2025, 1, 15)) == "2025-01-01 ~ 2025-01-15"
    assert format_time_range(date(2025, 1, 1), date(2025, 1, 1)) == "2025-01-01 ~ 2025-01-01"


# ── Node factory ──────────────────────────────────────────


class TestParseDateNode:
    FIXED_TODAY = date(2026, 8, 16)

    def _node(self, config=None):
        return make_parse_date(config, today=lambda: self.FIXED_TODAY)

    async def test_hit_returns_time_context(self):
        node = self._node()
        update = await node(make_state(question="最近7天有多少订单"))
        assert update == {"time_context": "2026-08-09 ~ 2026-08-16"}

    async def test_miss_passes_through(self):
        node = self._node()
        update = await node(make_state(question="每个学生的平均成绩"))
        assert update == {}

    async def test_upstream_error_short_circuits(self):
        node = self._node()
        update = await node(make_state(question="最近7天", error="upstream failed"))
        assert update == {}

    async def test_disabled_config_short_circuits(self):
        node = self._node(AgentConfig(date_parser=False))
        update = await node(make_state(question="最近7天有多少订单"))
        assert update == {}

    async def test_no_config_still_parses(self):
        """config=None (e.g. unbound GraphServices) still parses."""
        node = self._node(config=None)
        update = await node(make_state(question="今天有多少订单"))
        assert update == {"time_context": "2026-08-16 ~ 2026-08-16"}

    async def test_english_question_with_en_lang(self):
        node = self._node()
        update = await node(make_state(question="orders in the last 7 days", lang="en"))
        assert update == {"time_context": "2026-08-09 ~ 2026-08-16"}
