"""Prompt loader tests: language selection, fallback, interpolation, roster.

The roster smoke test renders every template in both languages so a missing
template file fails loudly here instead of at runtime.
"""

import pytest

from trove.prompts import render

# Every template name, in the shape "node/name". Adding a new prompt must
# also add it here (the roster test will fail otherwise).
ROSTER = [
    "gen_sql/system", "gen_sql/fix", "gen_sql/user",
    "planner/system", "planner/user",
    "reflect/system", "reflect/user", "reflect/reask_system", "reflect/reask_user",
    "analyze_error/system", "analyze_error/user",
    "answer/system", "answer/user",
    "metadata_check/system", "metadata_check/user",
    "intent/system",
    "kb/draft_system", "kb/init_system", "kb/draft_user", "kb/init_user", "kb/init_repair",
    "lesson_distill/system", "lesson_distill/user",
    "session/compact",
]


def test_render_system_bilingual():
    en = render("gen_sql/system", lang="en")
    zh = render("gen_sql/system", lang="zh")
    assert "You are a SQL generation assistant" in en
    assert "你是 SQL 生成助手" in zh


def test_render_falls_back_to_english():
    """lesson_distill ships only .en.j2 — a zh request falls back to en."""
    assert (
        render("lesson_distill/system", lang="zh")
        == render("lesson_distill/system", lang="en")
    )


def test_render_interpolates_variables():
    out = render("gen_sql/fix", lang="en", sql="SELEC 1", errors=["Parse error: bad"])
    assert "SELEC 1" in out
    assert "- Parse error: bad" in out


def test_conditional_block_rendered_only_when_present():
    with_history = render(
        "gen_sql/user", lang="en",
        question="q", schema_context="s", dialect="sqlite", history="h",
    )
    assert "Conversation history" in with_history

    without = render("gen_sql/user", lang="en", question="q", schema_context="s", dialect="sqlite")
    assert "Conversation history" not in without


def test_unknown_template_raises():
    with pytest.raises(ValueError):
        render("gen_sql/nonexistent", lang="en")


def test_invalid_name_rejected():
    with pytest.raises(ValueError):
        render("../evil", lang="en")


def test_full_roster_smoke():
    """Every template renders in both languages without raising."""
    for name in ROSTER:
        for lang in ("en", "zh"):
            out = render(name, lang=lang)
            assert isinstance(out, str) and out


class TestEstimationMirror:
    """context_budget 估算器的段文本必须与模板渲染一致（格式漂移会
    让 token 估算失真，此处钉死对齐）。"""

    SHOTS = [
        {"question": "q1", "sql": "SELECT 1"},
        {"question": "q2", "sql": "SELECT 2"},
    ]
    TERMS = [
        {"term": "t1", "mapping": "m1"},
        {"term": "t2", "mapping": "m2", "definition": "d2"},
    ]
    LESSONS = [
        {"pattern": "p1", "note": "n1"},
        {"pattern": "p2", "note": "n2"},
    ]
    RULES = ["rule1", "rule2"]

    def test_estimator_text_is_substring_of_rendered_prompt(self):
        from trove.workflow.nodes.gen_sql import (
            render_lessons, render_rules, render_shots, render_terms,
        )
        full = render(
            "gen_sql/user", lang="en",
            question="q", schema_context="s", dialect="sqlite",
            few_shots=self.SHOTS, term_notes=self.TERMS, lessons=self.LESSONS,
            rules=self.RULES,
        )
        assert render_shots(self.SHOTS) in full
        assert render_terms(self.TERMS) in full
        assert render_lessons(self.LESSONS) in full
        assert render_rules(self.RULES) in full

    def test_estimator_formats_match_template(self):
        """定义后缀（— definition）与行尾换行必须与模板逐字一致。"""
        from trove.workflow.nodes.gen_sql import render_terms
        assert "t2 → m2 — d2" in render_terms(self.TERMS)
        assert render_terms(self.TERMS).endswith("\n")
        assert render_terms([{"term": "t", "mapping": "m"}]) == "- t → m\n"

    def test_render_rules_matches_template(self):
        from trove.workflow.nodes.gen_sql import render_rules
        assert render_rules(["r1", "r2"]) == "- r1\n- r2\n"
        assert render_rules([]) == ""
