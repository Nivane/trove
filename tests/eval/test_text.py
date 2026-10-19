"""评测口径的单一来源:行为与旧实现逐字节相同。"""
from __future__ import annotations

import pytest

from trove.eval.text import classify_pred_error, norm_question, normalize_rows


def test_normalize_rows_sorts_and_stringifies():
    assert normalize_rows([[2, "a"], [1, None]]) == [("1", "None"), ("2", "a")]
    assert normalize_rows([[1.0]]) == [("1.0",)]


@pytest.mark.parametrize("raw,want", [
    ("Loan_Amount  by REGION", "loan amount by region"),  # \W_ 折下划线
    ("  按地区 统计 ", "按地区 统计"),
    (None, ""),
])
def test_norm_question_matches_old_gate_behavior(raw, want):
    assert norm_question(raw) == want


@pytest.mark.parametrize("error,want", [
    ("generation failed", "GENERATION_ERROR"),
    ("Generation timed out", "GENERATION_ERROR"),
    ("execution error: no such table", "EXECUTION_ERROR"),
    ("", "EXECUTION_ERROR"),
])
def test_classify_pred_error_matches_eval_bird(error, want):
    assert classify_pred_error(error) == want


def test_gate_private_alias_is_the_same_function():
    from trove.eval.gate import _norm_question
    assert _norm_question is norm_question
