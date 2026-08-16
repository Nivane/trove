"""Parse-date node — deterministic resolution of relative time expressions.

Pure-rule engine (no LLM): resolves "最近7天 / 上个月 / 上周 / 今年" (zh) and
"last 7 days / last month / this week" (en) into an absolute date range that
is injected into SQL generation, planning and reflection. Rules follow the
Datus date_parser design:

  - duration expressions ("最近N天") include the reference date
  - period expressions ("上个月") return the full calendar period only
  - zh weeks run Monday–Sunday; en weeks run Sunday–Saturday

Unmatched questions pass through silently (node returns {}), so the
pipeline behaves exactly as before for anything the rules don't cover.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

from trove.core.config import AgentConfig
from trove.workflow.state import WorkflowState

# ── Number helpers ────────────────────────────────────────

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

_EN_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def _cn_to_int(s: str) -> int | None:
    """Parse a Chinese numeral 0-99 ('两'=2, '二十三'=23); None if unsupported."""
    if s.isdigit():
        return int(s)
    if s in _CN_DIGITS:
        return _CN_DIGITS[s]
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        if right == "":
            return tens * 10
        if len(right) == 1 and right in _CN_DIGITS:
            return tens * 10 + _CN_DIGITS[right]
    return None


def _en_to_int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    return _EN_NUMBER_WORDS.get(s.lower())


# ── Date arithmetic ───────────────────────────────────────

def _shift_months(d: date, n: int) -> date:
    """d + n months, clamping the day to the target month's length."""
    month_index = d.year * 12 + (d.month - 1) + n
    year, month = divmod(month_index, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _week_start(d: date, lang: str) -> date:
    """Start of the week containing d: zh Monday, en Sunday."""
    if lang == "zh":
        return d - timedelta(days=d.weekday())
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _month_period(ref: date, delta: int) -> tuple[date, date]:
    first = date(ref.year, ref.month, 1)
    target = _shift_months(first, delta)
    return target, date(target.year, target.month, calendar.monthrange(target.year, target.month)[1])


def _year_period(ref: date, delta: int) -> tuple[date, date]:
    y = ref.year + delta
    return date(y, 1, 1), date(y, 12, 31)


def _duration(ref: date, n: int, unit: str, direction: int) -> tuple[date, date]:
    """Duration range: past (direction=-1) starts n units back and ends at ref;
    future (direction=+1) starts at ref. Weeks = 7 days, months clamped."""
    if unit in ("天", "日", "day", "days"):
        end = ref + timedelta(days=n * direction)
    elif unit in ("周", "星期", "week", "weeks"):
        end = ref + timedelta(days=7 * n * direction)
    elif unit in ("月", "month", "months"):
        end = _shift_months(ref, n * direction)
    else:  # 年 / year(s)
        end = _shift_months(ref, 12 * n * direction)
    return (end, ref) if direction < 0 else (ref, end)


def _match_first(text: str, alternatives: list[str]) -> str | None:
    """Return the earliest-occurring alternative in text (longer-first order)."""
    pos, found = len(text) + 1, None
    for alt in alternatives:
        idx = text.find(alt)
        if idx != -1 and idx < pos:
            pos, found = idx, alt
    return found


# ── Rule 1: composite ranges ("从X到Y" / "from X to Y") ───

_ZH_COMPOSITE_RE = re.compile(r"(?:从|自)\s*(.+?)\s*(?:到|至)\s*(.+)")
_ZH_NOW_RIGHT_RE = re.compile(r"^(?:现在|今天|今日|今)(?:的)?")

_EN_COMPOSITE_RE = re.compile(r"\bfrom\s+(.+?)\s+to\s+(.+)", re.IGNORECASE)
_EN_NOW_RIGHT_RE = re.compile(r"^(?:now|today)\b", re.IGNORECASE)


def _rule_composite_zh(text: str, ref: date) -> tuple[date, date] | None:
    m = _ZH_COMPOSITE_RE.search(text)
    if not m:
        return None
    left, right = m.group(1), m.group(2)
    if _ZH_NOW_RIGHT_RE.match(right):
        start = _parse_simple(left, ref, "zh")
        return (start[0], ref) if start else None
    left_range = _parse_simple(left, ref, "zh")
    right_range = _parse_simple(right, ref, "zh")
    if left_range and right_range:
        return left_range[0], right_range[1]
    return None  # one side is not a time expression — abandon, don't guess


def _rule_composite_en(text: str, ref: date) -> tuple[date, date] | None:
    m = _EN_COMPOSITE_RE.search(text)
    if not m:
        return None
    left, right = m.group(1), m.group(2)
    if _EN_NOW_RIGHT_RE.match(right):
        start = _parse_simple(left, ref, "en")
        return (start[0], ref) if start else None
    left_range = _parse_simple(left, ref, "en")
    right_range = _parse_simple(right, ref, "en")
    if left_range and right_range:
        return left_range[0], right_range[1]
    return None


# ── Rule 2: half-absolute ("2024年底到现在" / "end of 2024") ─

_ZH_HALF_ABS_RE = re.compile(r"(\d{4})\s*年\s*(底|末|初)")
_ZH_HALF_ABS_TAIL_RE = re.compile(r"^\s*(?:到|至)\s*(?:现在|今天|今日|今)")
_EN_HALF_ABS_RE = re.compile(r"\b(?:the\s+)?(end|beginning|start)\s+of\s+(\d{4})\b", re.IGNORECASE)


def _rule_half_abs_zh(text: str, ref: date) -> tuple[date, date] | None:
    m = _ZH_HALF_ABS_RE.search(text)
    if not m:
        return None
    year = int(m.group(1))
    d = date(year, 12, 31) if m.group(2) in ("底", "末") else date(year, 1, 1)
    rest = text[m.end():]
    if re.match(r"^\s*(?:到|至)", rest):
        # range tail ("...到2025年") — only interpret 到/至 + 现在/今天/今
        return (d, ref) if _ZH_HALF_ABS_TAIL_RE.match(rest) else None
    return d, d  # single date


def _rule_half_abs_en(text: str, ref: date) -> tuple[date, date] | None:
    m = _EN_HALF_ABS_RE.search(text)
    if not m:
        return None
    year = int(m.group(2))
    d = date(year, 12, 31) if m.group(1).lower() == "end" else date(year, 1, 1)
    return d, d


# ── Rule 3: day anchors ───────────────────────────────────

_ZH_DAY_ANCHORS = [("今天", 0), ("今日", 0), ("昨天", -1), ("昨日", -1), ("明天", 1), ("明日", 1)]
_EN_DAY_ANCHOR_RES = [
    (re.compile(r"\btoday\b", re.IGNORECASE), 0),
    (re.compile(r"\byesterday\b", re.IGNORECASE), -1),
    (re.compile(r"\btomorrow\b", re.IGNORECASE), 1),
]


def _rule_day_anchors_zh(text: str, ref: date) -> tuple[date, date] | None:
    word = _match_first(text, [w for w, _ in _ZH_DAY_ANCHORS])
    if word is None:
        return None
    delta = next(d for w, d in _ZH_DAY_ANCHORS if w == word)
    d = ref + timedelta(days=delta)
    return d, d


def _rule_day_anchors_en(text: str, ref: date) -> tuple[date, date] | None:
    for pattern, delta in _EN_DAY_ANCHOR_RES:
        if pattern.search(text):
            d = ref + timedelta(days=delta)
            return d, d
    return None


# ── Rules 4-6: calendar periods (week / month / year) ─────

_ZH_THIS_WEEK = ["这个星期", "本周", "这周"]
_ZH_LAST_WEEK = ["上个星期", "上星期", "上一周", "上周", "上礼拜"]
_ZH_NEXT_WEEK = ["下个星期", "下星期", "下一周", "下周", "下礼拜"]

_ZH_THIS_MONTH = ["这个月", "本月", "这月"]
_ZH_LAST_MONTH = ["上个月", "上月"]
_ZH_NEXT_MONTH = ["下个月", "下月"]

_ZH_THIS_YEAR = ["今年", "本年"]
_ZH_LAST_YEAR = ["去年", "上年"]
_ZH_NEXT_YEAR = ["明年", "来年"]

_EN_WEEK_RE = re.compile(r"\b(this|last|next)\s+week\b", re.IGNORECASE)
_EN_MONTH_RE = re.compile(r"\b(this|last|next)\s+month\b", re.IGNORECASE)
_EN_YEAR_RE = re.compile(r"\b(this|last|next)\s+year\b", re.IGNORECASE)


def _rule_periods_zh(text: str, ref: date) -> tuple[date, date] | None:
    start = _week_start(ref, "zh")
    if _match_first(text, _ZH_THIS_WEEK):
        return start, start + timedelta(days=6)
    if _match_first(text, _ZH_LAST_WEEK):
        return start - timedelta(weeks=1), start - timedelta(days=1)
    if _match_first(text, _ZH_NEXT_WEEK):
        return start + timedelta(weeks=1), start + timedelta(days=13)
    if _match_first(text, _ZH_THIS_MONTH):
        return _month_period(ref, 0)
    if _match_first(text, _ZH_LAST_MONTH):
        return _month_period(ref, -1)
    if _match_first(text, _ZH_NEXT_MONTH):
        return _month_period(ref, 1)
    for alternatives, delta in (
        (_ZH_THIS_YEAR, 0), (_ZH_LAST_YEAR, -1), (_ZH_NEXT_YEAR, 1),
    ):
        word = _match_first(text, alternatives)
        if word is None:
            continue
        after = text[text.find(word) + len(word):][:1]
        if after in ("底", "末", "初"):
            continue  # 「去年底/今年初」— half-absolute, out of v1 scope; don't guess
        return _year_period(ref, delta)
    return None


def _rule_periods_en(text: str, ref: date) -> tuple[date, date] | None:
    start = _week_start(ref, "en")
    if m := _EN_WEEK_RE.search(text):
        delta = {"this": 0, "last": -1, "next": 1}[m.group(1).lower()]
        w = start + timedelta(weeks=delta)
        return w, w + timedelta(days=6)
    if m := _EN_MONTH_RE.search(text):
        delta = {"this": 0, "last": -1, "next": 1}[m.group(1).lower()]
        return _month_period(ref, delta)
    if m := _EN_YEAR_RE.search(text):
        delta = {"this": 0, "last": -1, "next": 1}[m.group(1).lower()]
        return _year_period(ref, delta)
    return None


# ── Rules 7-8: durations (past / future) ──────────────────

_ZH_NUM = r"([0-9０-９一二两三四五六七八九十]+)"
_ZH_UNIT = r"(天|日|周|星期|月|年)"

_ZH_PAST_DUR_RE = re.compile(r"(?:最近|近|过去)\s*" + _ZH_NUM + r"\s*(?:个)?\s*" + _ZH_UNIT + r"(?:内|以内|之内)?")
_ZH_FUTURE_DUR_RE = re.compile(r"(?:未来|接下来|今后|往后)\s*" + _ZH_NUM + r"\s*(?:个)?\s*" + _ZH_UNIT + r"(?:内|以内|之内)?")

_EN_NUM = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_EN_UNIT = r"(day|days|week|weeks|month|months|year|years)"

_EN_PAST_DUR_RE = re.compile(
    r"\b(?:last|past|previous)\s+" + _EN_NUM + r"\s+" + _EN_UNIT + r"\b", re.IGNORECASE
)
_EN_FUTURE_DUR_RE = re.compile(
    r"\b(?:next|coming|following)\s+" + _EN_NUM + r"\s+" + _EN_UNIT + r"\b", re.IGNORECASE
)


def _rule_durations_zh(text: str, ref: date) -> tuple[date, date] | None:
    if m := _ZH_PAST_DUR_RE.search(text):
        n = _cn_to_int(m.group(1).translate(str.maketrans("０-９", "0-9")))
        if n is not None:
            return _duration(ref, n, m.group(2), direction=-1)
    if m := _ZH_FUTURE_DUR_RE.search(text):
        n = _cn_to_int(m.group(1).translate(str.maketrans("０-９", "0-9")))
        if n is not None:
            return _duration(ref, n, m.group(2), direction=1)
    return None


def _rule_durations_en(text: str, ref: date) -> tuple[date, date] | None:
    if m := _EN_PAST_DUR_RE.search(text):
        n = _en_to_int(m.group(1))
        if n is not None:
            return _duration(ref, n, m.group(2).lower(), direction=-1)
    if m := _EN_FUTURE_DUR_RE.search(text):
        n = _en_to_int(m.group(1))
        if n is not None:
            return _duration(ref, n, m.group(2).lower(), direction=1)
    return None


# ── Engine ────────────────────────────────────────────────

_ZH_RULES = [
    _rule_composite_zh,
    _rule_half_abs_zh,
    _rule_day_anchors_zh,
    _rule_periods_zh,
    _rule_durations_zh,
]

_EN_RULES = [
    _rule_composite_en,
    _rule_half_abs_en,
    _rule_day_anchors_en,
    _rule_periods_en,
    _rule_durations_en,
]


def _parse_simple(text: str, ref: date, lang: str) -> tuple[date, date] | None:
    """Run the non-composite rules (2-8) — used for composite side parsing."""
    rules = _ZH_RULES if lang == "zh" else _EN_RULES
    for rule in rules[1:]:
        result = rule(text, ref)
        if result is not None:
            return result
    return None


def parse_time_range(
    question: str,
    reference_date: date,
    lang: str = "zh",
) -> tuple[date, date] | None:
    """Resolve a relative time expression in the question to (start, end).

    First rule hit wins. Returns None when nothing matches (callers pass
    through silently). reference_date is a required positional so tests
    pin it to a fixed date.
    """
    rules = _ZH_RULES if lang == "zh" else _EN_RULES
    for rule in rules:
        result = rule(question, reference_date)
        if result is not None:
            return result
    return None


def format_time_range(start: date, end: date) -> str:
    """"YYYY-MM-DD ~ YYYY-MM-DD" (same value on both ends for a single date)."""
    return f"{start:%Y-%m-%d} ~ {end:%Y-%m-%d}"


# ── Node factory ──────────────────────────────────────────


def make_parse_date(
    config: AgentConfig | None = None,
    *,
    today: Callable[[], date] = date.today,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the parse-date node: deterministic, no LLM calls.

    Unmatched questions, upstream errors and a disabled date_parser config
    all return {} — the pipeline behaves exactly as before.
    """

    async def parse_date(state: WorkflowState) -> dict[str, Any]:
        if state.error:
            return {}
        if config is not None and not config.date_parser:
            return {}
        result = parse_time_range(state.question, today(), lang=state.lang)
        if result is None:
            return {}
        start, end = result
        return {"time_context": format_time_range(start, end)}

    return parse_date
