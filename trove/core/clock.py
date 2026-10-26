"""One UTC clock for persisted timestamps.

**Write side:** every persisted timestamp is aware UTC ISO-8601 with a
``+00:00`` suffix. Two runs recorded in different components are then
comparable as strings, and retention cutoffs mean the same instant everywhere.

**Read side tolerates the legacy era.** Before this module, decision /
action / scan / validate / jobs-handoff wrote ``datetime.now().isoformat()``
— naive *local* wall clocks. Those rows are not migrated (a rewrite of audit
history is a worse trade than a tolerant read): :func:`parse_utc` interprets
a naive value in the legacy zone, and SQL that filters on a timestamp takes
both cutoff forms through :func:`cutoff_pair`.

Why not ``datetime.utcnow()`` anywhere: it returns a *naive* value that merely
happens to be UTC — attaching a timezone later is a guess, and comparing it
with an aware value raises. The one clock is ``datetime.now(timezone.utc)``.

This module is pure stdlib and imports nothing from ``trove``; the architecture
invariant ``persisted-timestamps-utc`` scans it like any other writer.
"""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Any


def utc_now() -> datetime:
    """Current instant, aware UTC."""
    return datetime.now(timezone.utc)


def iso_utc(value: datetime, *, timespec: str = "seconds") -> str:
    """Render an **aware** datetime as UTC ISO-8601 (``...+00:00``).

    A naive value raises: it has no instant until someone decides which zone
    it was in, and that decision is exactly what this module exists to make
    explicit.
    """
    if value.tzinfo is None:
        raise ValueError("iso_utc() requires a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec=timespec)


def utc_now_iso(*, timespec: str = "seconds") -> str:
    """Current instant as UTC ISO-8601 — the canonical persisted form."""
    return iso_utc(utc_now(), timespec=timespec)


def parse_utc(
    value: Any, *, legacy_tz: tzinfo | None = None,
) -> datetime | None:
    """Parse a persisted timestamp into an aware UTC datetime.

    - aware values (``+00:00``, ``Z``, any offset) are normalized to UTC;
    - naive values are the **legacy local-clock era**, interpreted in
      ``legacy_tz`` (default: the running machine's local zone — DST-correct
      via ``astimezone()``; tests pass an explicit zone);
    - unparseable / empty / non-datetime values return ``None``.

    ``None`` means *cannot judge*, never *now*: retention and expiry decide
    what to do with an unreadable row, and guessing "now" is how a bad row
    silently keeps or loses history.
    """
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(value, datetime):
        parsed = value
    else:
        return None

    if parsed.tzinfo is None:
        if legacy_tz is None:
            parsed = parsed.astimezone()  # system local, DST-correct
        else:
            parsed = parsed.replace(tzinfo=legacy_tz)
    return parsed.astimezone(timezone.utc)


def cutoff_pair(
    cutoff: Any, *, legacy_tz: tzinfo | None = None,
) -> tuple[str, str] | None:
    """``(aware_utc_iso, legacy_naive_local_iso)`` for a mixed-era SQL cutoff.

    New rows end in ``+00:00``; legacy rows carry no offset suffix at all.
    Stores use the pair as two branches of one predicate::

        (ts LIKE '%+00:00' AND ts < ?) OR (ts NOT LIKE '%+00:00' AND ts < ?)

    The legacy form is the **same instant** on the legacy local clock — not
    the same digits. Returns ``None`` when the cutoff cannot be parsed; a
    caller must then skip the delete rather than purge on a guessed bound.
    """
    dt = parse_utc(cutoff, legacy_tz=legacy_tz)
    if dt is None:
        return None
    aware = dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    if legacy_tz is None:
        legacy = dt.astimezone().replace(tzinfo=None).isoformat(
            timespec="seconds")
    else:
        legacy = dt.astimezone(legacy_tz).replace(tzinfo=None).isoformat(
            timespec="seconds")
    return aware, legacy


def era_predicate(
    column: str, op: str, cutoff: Any, *, legacy_tz: tzinfo | None = None,
) -> tuple[str, tuple[str, str]] | None:
    """One SQL predicate that compares ``column`` against ``cutoff`` across eras.

    ``column`` is always a module-level constant (``expires_at``,
    ``attempted_at``, ``evaluated_at``) — never user input; the name is
    interpolated because SQL has no parameter for identifiers, and quoting it
    would give the name a second possible spelling.

    ``op`` is ``<``, ``>=`` or ``>``. Returns ``(sql, params)`` or ``None``
    when the cutoff is unparseable — callers must then *skip* the statement:
    an unreadable bound is "cannot judge", and both deleting and keeping on a
    guessed bound would be a decision nobody made.
    """
    if op not in ("<", ">=", ">"):
        raise ValueError(f"era_predicate: unsupported operator {op!r}")
    pair = cutoff_pair(cutoff, legacy_tz=legacy_tz)
    if pair is None:
        return None
    aware, legacy = pair
    sql = (f"(({column} LIKE '%+00:00' AND {column} {op} ?)"
           f" OR ({column} NOT LIKE '%+00:00' AND {column} {op} ?))")
    return sql, (aware, legacy)
