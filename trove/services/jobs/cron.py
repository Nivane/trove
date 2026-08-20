"""Minimal cron scheduling math — pure, dependency-free, deterministic.

Five-field cron:  minute hour day-of-month month day-of-week
  *        every value
  a-b      range
  a-b/n    range with step
  */n      step from field minimum
  a,b,c    list

Day-of-week 0 = Sunday, 7 = Sunday (both accepted). When both day-of-month
and day-of-week are restricted, a date matches if EITHER field matches
(standard vixie-cron semantics). Interval jobs ("every:30") carry a plain
minute step handled by ``interval_next``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


@dataclass(frozen=True)
class CronSchedule:
    """Parsed 5-field cron expression (minute/hour/dom/month/dow sets)."""

    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]

    @property
    def dom_full(self) -> bool:
        return self.dom == frozenset(range(1, 32))

    @property
    def dow_full(self) -> bool:
        # 0=Sunday..6=Saturday (7 alias collapsed to 0 at parse time)
        return self.dow == frozenset(range(0, 7))

    def matches(self, dt: datetime) -> bool:
        """True when the datetime satisfies this cron expression.

        Day matching follows vixie-cron: when both dom and dow are
        restricted the date matches if EITHER matches; when exactly one is
        restricted it is authoritative (the unrestricted field never widens
        the match back to "every day").
        """
        dow = dt.weekday() + 1  # 1=Monday..7=Sunday
        dw_ok = (dow % 7) in self.dow  # Sunday 7 -> 0
        if not self.dom_full and not self.dow_full:
            day_ok = (dt.day in self.dom) or dw_ok
        elif not self.dow_full:
            day_ok = dw_ok
        elif not self.dom_full:
            day_ok = dt.day in self.dom
        else:
            day_ok = True
        return (
            dt.month in self.month
            and day_ok
            and dt.hour in self.hour
            and dt.minute in self.minute
        )


def _expanded(
    field: str, low: int, high: int, allow_extra: int | None = None,
) -> frozenset[int]:
    """Expand one cron field into the matching value set.

    ``allow_extra`` adds a value beyond ``[low, high]`` accepted in the
    expression and remapped into the set (dow 7 → Sunday 0).
    """
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\*|\d+)(?:-(\d+))?(?:/(\d+))?", part)
        if not m:
            raise ValueError(f"invalid cron field: {part!r}")
        start_s, end_s, step_s = m.groups()
        step = int(step_s) if step_s else 1
        if step <= 0:
            raise ValueError(f"invalid step: {part!r}")
        if start_s == "*":
            lo, hi = low, high
        else:
            lo = int(start_s)
            hi = int(end_s) if end_s is not None else lo
        in_range = (low <= lo <= high and low <= hi <= high) or (
            allow_extra is not None and allow_extra in (lo, hi)
        )
        if not in_range or lo > hi:
            raise ValueError(f"out of range: {part!r}")
        values.update(range(lo, hi + 1, step))
    if not values:
        raise ValueError(f"empty cron field: {field!r}")
    if allow_extra is not None:
        values = {allow_extra if v == allow_extra else v for v in values}
    return frozenset(values)


def parse_cron(expr: str) -> CronSchedule | None:
    """Parse a 5-field cron expression; None when invalid."""
    if not expr:
        return None
    parts = expr.strip().split()
    if len(parts) != 5:
        return None
    try:
        minute, hour, dom, month = (  # dow gets Sunday-alias support below
            _expanded(p, *r) for p, r in zip(parts[:4], _FIELD_RANGES[:4])
        )
        dow = _expanded(parts[4], 0, 6, allow_extra=7)
    except (ValueError, AttributeError):
        return None
    # Normalize Sunday alias 7 -> 0
    dow = frozenset(0 if v == 7 else v for v in dow)
    return CronSchedule(minute=minute, hour=hour, dom=dom, month=month, dow=dow)


def cron_next(expr: str, now: datetime) -> datetime | None:
    """Next datetime strictly after ``now`` matching the cron (None if invalid).

    Day-stepping search is bounded to 8 years; a valid but unreachable
    schedule (e.g. Feb 30) returns None rather than looping forever.
    """
    try:
        cron = parse_cron(expr)
    except Exception:
        return None
    if cron is None:
        return None
    probe = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = now + timedelta(days=365 * 8)
    while probe <= limit:
        if cron.matches(probe):
            return probe
        probe += timedelta(minutes=1)
    return None


def interval_next(minutes: int, now: datetime) -> datetime | None:
    """Next occurrence for an interval job (minutes >= 1)."""
    if minutes < 1:
        return None
    return now + timedelta(minutes=minutes)