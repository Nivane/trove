"""Synthetic few-shot (SQL-to-Text) tests.

零网络/零金标:LLM 全部 mock,护栏校验落在 sqlite_registry 上。
"""

import pytest

from trove.services.kb.synthetic import (
    generate_synthetic_examples,
    schema_text,
    stats_suffix,
    validate_examples,
    _parse_synthetic,
)

TABLE = [{
    "name": "students",
    "description": "",
    "columns": [
        {"name": "id", "type": "int", "description": "student identifier", "enums": []},
        {"name": "grade", "type": "int", "description": "grade", "enums": []},
        {"name": "county", "type": "varchar", "description": "county", "enums": []},
    ],
    "metrics": [],
}]

STATS = {
    "students": {
        "row_count": 5,
        "columns": {
            "grade": {"null_ratio": 0.0, "distinct": 5, "shape": "text",
                      "min": 75, "max": 99},
            "county": {"null_ratio": 0.0, "distinct": 3, "shape": "text"},
        },
    },
}

SAMPLES = {"students": {"county": "Alameda; Orange; Los Angeles"}}

GOOD_JSON = """{"examples": [
  {"question": "Average grade by county",
   "sql": "SELECT county, AVG(grade) FROM students GROUP BY county",
   "tags": ["students", "aggregation"]},
  {"question": "Top 2 students by grade",
   "sql": "SELECT name, grade FROM students ORDER BY grade DESC LIMIT 2",
   "tags": ["students", "order"]}
]}"""


class TestStatsSuffix:
    def test_renders_only_notable_bits(self):
        assert stats_suffix({"null_ratio": 0.9, "distinct": 3, "shape": "json",
                             "min": 1, "max": 100}) == " [90% NULL, 3 distinct, json, 1..100]"

    def test_range_without_shape_bits(self):
        assert stats_suffix({"min": 75, "max": 99}) == " [75..99]"

    def test_length_bits(self):
        assert stats_suffix({"min_len": 14, "max_len": 17}) == " [14-17 chars]"

    def test_trivial_stats_renders_nothing(self):
        assert stats_suffix({"null_ratio": 0.0, "shape": "text"}) == ""

    def test_none_renders_nothing(self):
        assert stats_suffix(None) == ""


class TestSchemaText:
    def test_row_count_header_and_stats(self):
        text = schema_text(TABLE, stats=STATS)
        assert "students (5 rows):" in text
        assert "grade int — grade [5 distinct, 75..99]" in text

    def test_samples_shown(self):
        text = schema_text(TABLE, samples=SAMPLES)
        assert "[Alameda; Orange; Los Angeles]" in text

    def test_no_stats_plain(self):
        text = schema_text(TABLE)
        assert "students:" in text
        assert "(5 rows)" not in text
        assert "[75..99]" not in text


class TestParse:
    def test_valid_json(self):
        out = _parse_synthetic(GOOD_JSON)
        assert len(out) == 2
        assert out[0]["question"] == "Average grade by county"
        assert out[0]["sql"].startswith("SELECT county")
        assert out[0]["tags"] == ["students", "aggregation"]

    def test_fenced_json(self):
        out = _parse_synthetic("```json\n" + GOOD_JSON + "\n```")
        assert len(out) == 2

    def test_garbage_returns_empty(self):
        assert _parse_synthetic("still not json") == []
        assert _parse_synthetic("") == []

    def test_wrong_shape_returns_empty(self):
        assert _parse_synthetic('{"not_examples": []}') == []
        assert _parse_synthetic('{"examples": "nope"}') == []
        assert _parse_synthetic("[1, 2, 3]") == []

    def test_drops_entries_missing_question_or_sql(self):
        out = _parse_synthetic("""{"examples": [
          {"question": "", "sql": "SELECT 1"},
          {"question": "q", "sql": ""},
          {"question": "q", "sql": "SELECT 1", "tags": ["a", "b", "c", "d", "e", "f", "g"]}
        ]}""")
        assert len(out) == 1
        assert out[0]["tags"] == ["a", "b", "c", "d", "e", "f"]  # tags 截断


class TestValidateExamples:
    @pytest.mark.asyncio
    async def test_sqlglot_rejects_invalid_syntax(self, sqlite_registry):
        examples = [
            {"question": "bad", "sql": "SELECT FROM students"},
            {"question": "good", "sql": "SELECT grade FROM students"},
        ]
        kept = await validate_examples(examples, sqlite_registry, sqlite_registry.default_name)
        assert len(kept) == 1
        assert kept[0]["question"] == "good"

    @pytest.mark.asyncio
    async def test_trial_execution_rejects_unknown_columns(self, sqlite_registry):
        examples = [
            {"question": "nope", "sql": "SELECT nope FROM students"},
            {"question": "ok", "sql": "SELECT name FROM students"},
        ]
        kept = await validate_examples(examples, sqlite_registry, sqlite_registry.default_name)
        assert len(kept) == 1
        assert kept[0]["question"] == "ok"

    @pytest.mark.asyncio
    async def test_all_kept_stamped_template_true(self, sqlite_registry):
        examples = [
            {"question": "avg", "sql": "SELECT AVG(grade) FROM students", "tags": []},
            {"question": "count", "sql": "SELECT COUNT(*) FROM students", "tags": []},
        ]
        kept = await validate_examples(examples, sqlite_registry, sqlite_registry.default_name)
        assert len(kept) == 2
        assert all(ex.get("template") is True for ex in kept)

    @pytest.mark.asyncio
    async def test_registry_none_skips_trial_keeps_sqlglot(self):
        examples = [{"question": "ok", "sql": "SELECT 1"}]
        kept = await validate_examples(examples, None, None)
        assert len(kept) == 1


class TestGenerate:
    @pytest.mark.asyncio
    async def test_prompt_carries_schema_stats_and_samples(self):
        class LLM:
            def __init__(self):
                self.calls = []

            async def chat(self, model, messages, **kwargs):
                self.calls.append((model, messages, kwargs))
                return GOOD_JSON

        llm = LLM()
        out = await generate_synthetic_examples(
            llm, "mock/model", TABLE, samples=SAMPLES, stats=STATS,
        )
        assert len(out) == 2
        model, messages, kwargs = llm.calls[0]
        assert model == "mock/model"
        assert kwargs["max_tokens"] == 8192
        joined = " ".join(m["content"] for m in messages)
        assert "students (5 rows)" in joined
        assert "5 distinct, 75..99" in joined
        assert "Alameda" in joined

    @pytest.mark.asyncio
    async def test_unusable_response_returns_empty(self):
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "not json at all"

        out = await generate_synthetic_examples(LLM(), "mock/model", TABLE, stats=STATS)
        assert out == []

    @pytest.mark.asyncio
    async def test_empty_tables_returns_empty_without_llm_call(self):
        class LLM:
            async def chat(self, model, messages, **kwargs):
                raise AssertionError("must not be called")

        assert await generate_synthetic_examples(LLM(), "mock/model", []) == []

    @pytest.mark.asyncio
    async def test_generate_then_validate_end_to_end(self, sqlite_registry):
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return GOOD_JSON

        generated = await generate_synthetic_examples(
            LLM(), "mock/model", TABLE, stats=STATS,
        )
        kept = await validate_examples(generated, sqlite_registry, sqlite_registry.default_name)
        assert len(kept) == 2
        assert all(ex["template"] is True for ex in kept)
