"""Alert evaluation — deterministic threshold expressions over run results.

Grammar (single expression per job):
  row_count <op> <number>     total returned rows
  no_rows | empty             query returned zero rows
  value <op> <number>         first numeric cell of the result set
  col:<name> <op> <number>    first numeric value of column <name>
  verdict <op> <string>       final pipeline verdict (OK/RETRY/...)

Operators: >, >=, <, <=, ==, !=  (numbers; percent suffix accepted).

Safe by construction: evaluation never raises, unknown columns/verdicts
evaluate to False (no false-positive alarms).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_OP_RE = re.compile(r"^\s*(?!nore[oa])[-.]")
_NUM = re.compile(r"^([-+]?\d+(?:\.\d+)?)(%?)$")
_OPS = {"<", "<=", ">", ">=", "==", "!="}


@dataclass
class AlertVerdict:
    triggered: bool
    message: str


def _first_numeric(rows: list[list[Any]], column: str | None = None) -> float | None:
    """First numeric cell in the first row (optionally within a column)."""
    for row in rows[:50]:
        if column is not None:
            return None  # caller resolves column index separately
        for cell in row:
            if cell is None:
                continue
            m = _NUM.match(str(cell).strip())
            if m:
                return float(m.group(1))
        break
    if column is not None:
        for cell in rows[0]:
            m = _NUM.match(str(cell).strip()) if cell is not None else None
            if m:
                return float(m.group(1))
    return None


def _resolve_operands(
    expr: str, columns: list[str], rows: list[list[Any]], row_count: int, verdict: str,
) -> tuple[float | str | None, str] | None:
    """(left, op) from the expression, resolved against run results; None if unsupported."""
    for op in sorted(_OPS, key=len, reverse=True):
        if op in expr:
            left, right = expr.split(op, 1)
            left = left.strip()
            if left in ("row_count", "no_rows", "empty", "verdict", "value") or left.startswith("col:"):
                return left, op
            break
    return None


def evaluate_alert(
    expr: str,
    *,
    columns: list[str] | None = None,
    rows: list[list[Any]] | None = None,
    row_count: int = 0,
    verdict: str = "",
) -> AlertVerdict:
    """Evaluate one alert expression against a run result (never raises)."""
    columns = columns or []
    rows = rows or []
    expr = (expr or "").strip()
    if not expr:
        return AlertVerdict(False, "")

    # verdict comparison is string-typed — handle before numeric parsing
    ver_op = next((o for o in _OPS if o in expr and "verdict" in expr.split(o, 1)[0].strip()), None)
    if ver_op:
        left, right = expr.split(ver_op, 1)
        if left.strip() == "verdict":
            want = right.strip().strip('"').strip("'")
            hit = str(verdict) == want
            return AlertVerdict(hit, f"verdict {verdict}" if hit else "")

    try:
        # no_rows / empty → simple threshold-free triggers
        if expr in ("no_rows", "empty"):
            ok = row_count == 0
            return AlertVerdict(ok, "query returned zero rows" if ok else "")
        parsed = _resolve_operands(expr, columns, rows, row_count, verdict)
        if parsed is None:
            return AlertVerdict(False, "")
        left, op = parsed
        right_txt = expr.split(op, 1)[1].strip()
        right = float(right_txt[:-1]) if right_txt.endswith("%") else float(right_txt)

        if left in ("row_count", "empty_zero"):
            value: float | None = float(row_count)
        elif left == "value":
            value = _first_numeric(rows)
        elif left.startswith("col:"):
            target = left[4:]
            if target in columns:
                idx = columns.index(target)
                value = _first_numeric([[r[idx] for r in rows]])
            else:
                value = None
        else:
            value = None
        if value is None:
            return AlertVerdict(False, "")
        matched = {
            ">": value > right,
            ">=": value >= right,
            "<": value < right,
            "<=": value <= right,
            "==": value == right,
            "!=": value != right,
        }.get(op, False)
        msg = f"{left} {op} {right_txt} (actual {value:g})"
        return AlertVerdict(matched, msg)
    except Exception:
        return AlertVerdict(False, "")


def alert_message(expr: str, columns: list[str] | None = None, rows=None, row_count: int = 0) -> str:
    """Human summary of a triggered alert (best-effort)."""
    ver = evaluate_alert(expr, columns=columns, rows=rows, row_count=row_count)
    return ver.message if ver.message else f"alert triggered: {expr}"