"""Deterministic time-period math — current window → baseline window.

Pure functions, zero LLM, zero I/O. Extracted from the attribution node so
any layer that needs "compare period A against period B" (attribution,
decision rules) shares one definition of 环比/同比 instead of growing a
third copy of the calendar arithmetic.

The window string format is owned by ``workflow/nodes/parse_date.py``;
``format_time_range`` produces it and ``base_period`` consumes it:

    format_time_range(*parse_time_range("本月", reference_date=today))
    # → "2026-09-01 ~ 2026-09-30"
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

_RANGE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})$")

#: Baseline kinds ``base_period`` understands. ``share`` is deliberately
#: absent from callers that must not silently lose their baseline (the
#: decision layer) — it means "no baseline at all", which is a different
#: question from "环比/同比".
BASELINE_KINDS = ("prev_period", "yoy", "share")


def shift_months(d: date, n: int) -> date:
    """d + n months, day clamped to the target month's length.

    Same convention as ``parse_date``: 2026-03-31 minus 1 month is
    2026-02-28, not an invalid date.
    """
    month_index = d.year * 12 + (d.month - 1) + n
    year, month = divmod(month_index, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def base_period(
    time_context: str,
    baseline: str,
) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """当前期 + 基期(从 parse_date 的 time_context 确定性派生)。

    time_context: "YYYY-MM-DD ~ YYYY-MM-DD"。基期派生:
      - prev_period:往前推一个等长窗口(环比);
      - yoy:往前推 1 年(同比,月/日钳制);
      - share:无基期(占比归因,返回 None)。

    Returns ``((cur_start, cur_end), (base_start, base_end))``, or None when
    the window is unparseable **or** ``baseline == "share"`` — callers must
    tell those two apart from their own call site (the decision layer never
    passes ``share``, so a None there is always a real failure).
    """
    m = _RANGE_RE.match((time_context or "").strip())
    if not m:
        return None
    try:
        start = date.fromisoformat(m.group(1))
        end = date.fromisoformat(m.group(2))
    except ValueError:
        return None
    if baseline == "share":
        return None
    if baseline == "yoy":
        base_start = shift_months(start, -12)
        base_end = shift_months(end, -12)
    else:  # prev_period
        span = (end - start).days + 1
        base_end = start - timedelta(days=1)
        base_start = base_end - timedelta(days=span - 1)
    return (
        (start.isoformat(), end.isoformat()),
        (base_start.isoformat(), base_end.isoformat()),
    )
