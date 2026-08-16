"""Lesson distillation tests — eval failures into Hint Bank lessons."""

from trove.services.kb.lesson_distill import (
    build_distill_prompt,
    dedupe_by_pattern,
    parse_lesson,
)


FAILURE = {
    "question": "Among the accounts who have approved loan date in 1997, "
                "list out the accounts that have the lowest approved amount "
                "and choose weekly issuance statement.",
    "evidence": "'POPLATEK TYDNE' stands for weekly issuance",
    "gold_sql": "SELECT account_id FROM loan JOIN account ... ORDER BY amount LIMIT 1",
    "pred_sql": "SELECT a.account_id FROM loan l ... WHERE amount = (SELECT MIN(amount) ...)",
    "error": "mismatch",
}


class TestParseLesson:
    def test_parses_plain_json(self):
        lesson = parse_lesson(
            '{"pattern": "lowest approved amount", "note": "先过滤再取极值", '
            '"sql_snippet": "WHERE amount = (SELECT MIN"}'
        )
        assert lesson["pattern"] == "lowest approved amount"
        assert lesson["note"] == "先过滤再取极值"

    def test_parses_fenced_json(self):
        lesson = parse_lesson(
            '```json\n{"pattern": "weekly issuance", "note": "周发放是限定条件", '
            '"sql_snippet": ""}\n```'
        )
        assert lesson["pattern"] == "weekly issuance"

    def test_invalid_responses_return_none(self):
        assert parse_lesson("这里是一段散文教训") is None
        assert parse_lesson('{"note": "只有 note 没有 pattern"}') is None
        assert parse_lesson('{"pattern": "", "note": ""}') is None

    def test_fields_trimmed(self):
        lesson = parse_lesson(
            '{"pattern": "  x  ", "note": "y", "sql_snippet": "z"}'
        )
        assert lesson == {"pattern": "x", "note": "y", "sql_snippet": "z"}


class TestDedupe:
    def test_skips_existing_patterns_case_insensitive(self):
        existing = [{"pattern": "Lowest approved amount"}, {"pattern": "weekly"}]
        fresh = dedupe_by_pattern(
            [
                {"pattern": "lowest approved amount", "note": "a"},
                {"pattern": "choose weekly issuance", "note": "b"},
            ],
            existing,
        )
        assert fresh == [{"pattern": "choose weekly issuance", "note": "b"}]

    def test_duplicates_within_batch_skipped(self):
        fresh = dedupe_by_pattern(
            [{"pattern": "x", "note": "a"}, {"pattern": "X", "note": "b"}], [],
        )
        assert len(fresh) == 1


class TestPrompt:
    def test_prompt_carries_question_gold_and_pred(self):
        prompt = build_distill_prompt(FAILURE)
        assert FAILURE["question"][:40] in prompt
        assert FAILURE["gold_sql"] in prompt
        assert FAILURE["pred_sql"] in prompt
        assert "mismatch" in prompt
