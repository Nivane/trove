"""Deterministic result/SQL validation rules (no LLM involved).

Rule failures route back to gen_sql through the shared error_feedback
correction channel — the principle: what can be checked by code should
not be left to an LLM judge. This module is the mounting point for
business-caliber rules (semantic-layer constraints) as they emerge.

Patterns are deliberately narrow to avoid false positives; questions
matching no rule pass through untouched.
"""

from __future__ import annotations

import re

_TOP = r"(?:\d+|[一二三四五六七八九十]+|ten|nine|eight|seven|six|five|four|three|two|one)"

_COUNT_PATTERNS = [
    re.compile(r"\bhow many\b", re.I),
    re.compile(r"\bnumber of\b", re.I),
    re.compile(r"\bwhat is the total\b", re.I),
    re.compile(r"有多少|多少个|总数|总数量"),
]
# grouped counts ("how many X per Y", "哪个地区…总数", "each branch") are not
# single-value count questions
_COUNT_GROUP_GUARD = re.compile(
    r"\bper\b|group by|每个|按.{0,6}分|哪个|哪些|\bwhich\b|\beach\b", re.I,
)

# questions asking for several metrics at once ("总数和平均") are not counts
_MULTI_METRIC_GUARD = re.compile(
    r"总数.{0,8}(?:平均|最大|最小|最高|最低)|(?:平均|最大|最小|最高|最低).{0,8}总数",
    re.I,
)

_LIST_PATTERNS = [
    re.compile(r"\blist\b", re.I),
    re.compile(r"\bwhich\b", re.I),
    re.compile(rf"\btop\s*{_TOP}\b", re.I),
    re.compile(rf"列出|哪些|前\s*{_TOP}"),
]

_PERCENT_PATTERNS = [
    re.compile(r"percent", re.I),
    re.compile(r"百分比|占比|比例"),
]

_ORDERED_PATTERNS = [
    re.compile(rf"\btop\s*{_TOP}\b", re.I),
    re.compile(rf"前\s*{_TOP}"),
    re.compile(r"排名"),
]

_LIMIT_RE = re.compile(r"\blimit\b", re.I)
_ORDER_BY_RE = re.compile(r"\border\s+by\b", re.I)


def _matches(patterns: list[re.Pattern], question: str) -> bool:
    return any(p.search(question) for p in patterns)


def is_count_question(question: str) -> bool:
    """Single-value count question (excludes grouped and multi-metric shapes).

    List questions win on precedence: "List the no. of X" is a list
    question whose gold result is many rows, not a single count.
    Grouped shapes ("哪个地区…总数", "each branch") and multi-metric
    conjunctions ("总数和平均") are excluded too.
    """
    if _matches(_LIST_PATTERNS, question):
        return False
    if _COUNT_GROUP_GUARD.search(question) or _MULTI_METRIC_GUARD.search(question):
        return False
    return _matches(_COUNT_PATTERNS, question)


def is_list_question(question: str) -> bool:
    """List/top-N question expecting multiple rows."""
    return _matches(_LIST_PATTERNS, question)


def is_percent_question(question: str) -> bool:
    """Percentage/proportion question (result should be a 0-100 number)."""
    return _matches(_PERCENT_PATTERNS, question)


def is_ordered_question(question: str) -> bool:
    """Question whose result order matters (top-N / ranking)."""
    return _matches(_ORDERED_PATTERNS, question)


def validate(
    question: str,
    sql: str,
    columns: list,
    rows: list[list],
    row_count: int,
) -> str | None:
    """Run deterministic rules; return a failure reason, or None to pass.

    Args:
        question: The user question.
        sql: The SQL that produced the result.
        columns: Result column names.
        rows: Result rows.
        row_count: Number of result rows.
    """
    # 1. count questions must return a single number
    if is_count_question(question):
        if row_count != 1 or len(columns) != 1 or not rows or len(rows[0]) != 1:
            return "count question should return a single number (single row, single column)"
        if rows[0][0] is None:
            return "count question returned NULL"

    # 2. list/top-N questions returning zero rows are almost always wrong
    if is_list_question(question) and row_count == 0:
        return "list question returned no rows — check joins/filters/table names"

    # 3. percentage questions must produce a numeric value in [0, 100]
    if is_percent_question(question) and row_count == 1 and rows and len(rows[0]) == 1:
        value = rows[0][0]
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "percentage result should be a numeric value"
        if not 0 <= numeric <= 100:
            return "percentage result out of 0-100 range"

    # 4. top-N/ranking questions with LIMIT need ORDER BY
    if (
        is_ordered_question(question)
        and _LIMIT_RE.search(sql)
        and not _ORDER_BY_RE.search(sql)
    ):
        return "top-N query uses LIMIT without ORDER BY"

    return None
