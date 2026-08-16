"""eval_bird slicing semantics: skip (offset) first, then limit.

`--start 5 --limit 5` must mean "skip the first 5, evaluate the next 5"
(limit + offset), not "take 5 then skip them" (0 questions).
"""

from scripts.eval_bird import slice_questions


def _qs(n=10):
    return [{"q": i} for i in range(n)]


def test_limit_and_offset():
    """start 5 + limit 5 → 第 6~10 题。"""
    assert slice_questions(_qs(), limit=5, start=5) == [{"q": i} for i in range(5, 10)]


def test_offset_only():
    assert slice_questions(_qs(), start=5) == [{"q": i} for i in range(5, 10)]


def test_limit_only():
    assert slice_questions(_qs(), limit=3) == [{"q": 0}, {"q": 1}, {"q": 2}]


def test_no_bounds():
    assert slice_questions(_qs()) == _qs()


def test_offset_beyond_length():
    assert slice_questions(_qs(), start=99) == []


def test_limit_zero_means_all():
    assert slice_questions(_qs(), limit=0, start=5) == [{"q": i} for i in range(5, 10)]
