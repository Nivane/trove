"""Workflow node function tests (LangGraph era).

Nodes are plain async functions `async def node(state) -> dict` that
return a partial state update. Services are bound at construction time
via factory functions.
"""

import pytest

from trove.core.config import AgentConfig
from trove.workflow.state import WorkflowState
from trove.llm.gateway import LLMGateway

from trove.workflow.nodes.schema_linking import make_schema_linking
from trove.workflow.nodes.gen_sql import (
    build_fix_prompt,
    build_sql_prompt,
    extract_sql,
    make_generate,
    make_validate,
    validate_sql,
)
from trove.workflow.nodes.execute_sql import make_execute_sql
from trove.workflow.nodes.reflect import make_reflect
from trove.workflow.nodes.output import output


def make_state(**kwargs) -> WorkflowState:
    defaults = {"session_id": "s1", "question": "Average grade by county"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


class ScriptedLLM:
    """LLM mock that returns scripted responses and records prompts."""

    def __init__(self, responses):
        self._responses = iter(responses)
        self.last_messages = []

    async def chat(self, model, messages, **kwargs):
        self.last_messages = messages
        return next(self._responses)


# ── Schema Linking ───────────────────────────────────────


class TestSchemaLinking:
    async def test_no_catalog_graceful(self):
        node = make_schema_linking(catalog=None)
        update = await node(make_state())
        assert update["matched_tables"] == []
        assert "No schema" in update["schema_context"]

    async def test_with_catalog(self, catalog):
        node = make_schema_linking(catalog=catalog)
        update = await node(make_state())
        assert "students" in update["matched_tables"]
        assert "grade" in update["schema_context"]

    async def test_catalog_failure_sets_error(self):
        class BrokenCatalog:
            async def search_tables(self, *a, **k):
                raise RuntimeError("catalog down")

        node = make_schema_linking(catalog=BrokenCatalog())
        update = await node(make_state())
        assert "Schema linking failed" in update["error"]

    async def test_error_passthrough(self, catalog):
        """A node must not run when an upstream node already failed."""
        node = make_schema_linking(catalog=catalog)
        update = await node(make_state(error="upstream failed"))
        assert update == {}


# ── gen_sql: prompt builders and extraction ──────────────


class TestSQLHelpers:
    def test_extract_sql_from_code_block(self):
        response = "Here is the query:\n```sql\nSELECT * FROM students;\n```\nHope it helps!"
        assert extract_sql(response) == "SELECT * FROM students;"

    def test_extract_sql_generic_block(self):
        assert extract_sql("```\nSELECT 1\n```") == "SELECT 1"

    def test_extract_sql_raw(self):
        response = "SELECT county, AVG(grade) FROM students GROUP BY county"
        assert extract_sql(response).startswith("SELECT")

    def test_extract_empty(self):
        assert extract_sql("") == ""
        assert extract_sql("I cannot generate SQL for this") != ""

    def test_build_sql_prompt_includes_reflect_reason(self):
        prompt = build_sql_prompt("q", "schema", "sqlite", reflect_reason="wrong grouping")
        assert "wrong grouping" in prompt
        assert "rejected" in prompt

    def test_build_fix_prompt_lists_errors(self):
        prompt = build_fix_prompt("SELEC 1", ["Parse error: bad"])
        assert "SELEC 1" in prompt
        assert "Parse error: bad" in prompt

    def test_validate_sql_valid(self):
        valid, errors = validate_sql("SELECT * FROM t", "sqlite")
        assert valid is True
        assert errors == []

    def test_validate_sql_invalid(self):
        valid, errors = validate_sql("SELEC * FROM", "sqlite")
        assert valid is False
        assert len(errors) > 0


# ── gen_sql subgraph nodes: generate / validate ──────────


class TestGenerate:
    def _config(self):
        return AgentConfig(target="mock/model")

    async def test_returns_sql_and_increments_attempts(self):
        llm = ScriptedLLM(["```sql\nSELECT 1;\n```"])
        generate = make_generate(llm, self._config())
        update = await generate(
            __import__("trove.workflow.state", fromlist=["GenSQLState"]).GenSQLState(
                question="q", schema_context="", dialect="sqlite",
            )
        )
        assert update["sql"] == "SELECT 1;"
        assert update["attempts"] == 1
        assert update["validation_errors"] == []

    async def test_uses_fix_prompt_after_validation_errors(self):
        llm = ScriptedLLM(["```sql\nSELECT 1;\n```"])
        generate = make_generate(llm, self._config())
        from trove.workflow.state import GenSQLState
        state = GenSQLState(
            question="q", schema_context="", dialect="sqlite",
            sql="SELEC 1", attempts=1, validation_errors=["Parse error"],
        )
        update = await generate(state)
        assert update["attempts"] == 2
        assert "failed validation" in llm.last_messages[-1]["content"]

    async def test_skips_when_error_present(self):
        class RaisingLLM:
            async def chat(self, *a, **k):
                raise AssertionError("LLM must not be called")

        generate = make_generate(RaisingLLM(), self._config())
        from trove.workflow.state import GenSQLState
        state = GenSQLState(question="q", schema_context="", dialect="sqlite", error="upstream")
        assert await generate(state) == {}

    async def test_includes_reflect_reason_on_first_pass(self):
        llm = ScriptedLLM(["```sql\nSELECT 1;\n```"])
        generate = make_generate(llm, self._config())
        from trove.workflow.state import GenSQLState
        state = GenSQLState(
            question="q", schema_context="", dialect="sqlite", reflect_reason="wrong grouping",
        )
        await generate(state)
        assert "wrong grouping" in llm.last_messages[-1]["content"]


class TestValidate:
    async def test_valid_sql_clears_errors(self):
        from trove.workflow.state import GenSQLState
        validate = make_validate(max_retries=3)
        state = GenSQLState(question="q", dialect="sqlite", sql="SELECT 1", attempts=1)
        update = await validate(state)
        assert update["validation_errors"] == []

    async def test_invalid_sql_records_errors(self):
        from trove.workflow.state import GenSQLState
        validate = make_validate(max_retries=3)
        state = GenSQLState(question="q", dialect="sqlite", sql="SELEC 1", attempts=1)
        update = await validate(state)
        assert len(update["validation_errors"]) > 0

    async def test_empty_sql_records_error(self):
        from trove.workflow.state import GenSQLState
        validate = make_validate(max_retries=3)
        state = GenSQLState(question="q", dialect="sqlite", sql="", attempts=1)
        update = await validate(state)
        assert "empty sql" in " ".join(update["validation_errors"]).lower()

    async def test_exhausted_attempts_sets_error(self):
        from trove.workflow.state import GenSQLState
        validate = make_validate(max_retries=3)
        state = GenSQLState(
            question="q", dialect="sqlite", sql="SELEC 1", attempts=3,
        )
        update = await validate(state)
        assert "3 attempts" in update["error"]

    async def test_skips_when_error_present(self):
        from trove.workflow.state import GenSQLState
        validate = make_validate(max_retries=3)
        state = GenSQLState(question="q", dialect="sqlite", sql="SELECT 1", attempts=1, error="upstream")
        assert await validate(state) == {}


# ── Execute SQL ──────────────────────────────────────────


class TestExecuteSQL:
    async def test_no_sql_sets_error(self):
        node = make_execute_sql()
        update = await node(make_state())
        assert "No SQL" in update["error"]

    async def test_execute_valid_sql(self, sqlite_registry):
        node = make_execute_sql(sqlite_registry)
        state = make_state(sql="SELECT name FROM students ORDER BY name")
        update = await node(state)
        assert update["row_count"] == 5
        assert update["columns"] == ["name"]

    async def test_execute_error_sql(self, sqlite_registry):
        node = make_execute_sql(sqlite_registry)
        update = await node(make_state(sql="SELECT * FROM nonexistent"))
        assert update["error"]

    async def test_error_passthrough(self, sqlite_registry):
        node = make_execute_sql(sqlite_registry)
        update = await node(make_state(sql="SELECT 1", error="upstream failed"))
        assert update == {}


# ── Reflect ──────────────────────────────────────────────


class TestReflect:
    def _make(self, response="OK"):
        llm = LLMGateway(mock_response=response)
        return make_reflect(llm, AgentConfig(target="mock/model"))

    async def test_empty_result_short_circuits(self):
        class NoCallLLM:
            async def chat(self, *a, **k):
                raise AssertionError("LLM must not be called for empty results")

        node = make_reflect(NoCallLLM(), AgentConfig(target="mock/model"))
        update = await node(make_state(row_count=0))
        assert update["verdict"] == "EMPTY"

    async def test_ok_verdict(self):
        node = self._make("OK")
        update = await node(make_state(row_count=3, columns=["x"], rows=[[1], [2], [3]]))
        assert update["verdict"] == "OK"

    async def test_retry_verdict(self):
        node = self._make("RETRY: wrong grouping")
        update = await node(make_state(row_count=3, columns=["x"], rows=[[1], [2], [3]]))
        assert update["verdict"] == "RETRY"
        assert update["reason"] == "wrong grouping"
        assert update["retry_count"] == 1

    async def test_retry_cap_forces_ok(self):
        """At the retry cap a RETRY verdict is forced to OK."""
        node = self._make("RETRY: still wrong")
        update = await node(
            make_state(row_count=3, columns=["x"], rows=[[1], [2], [3]], retry_count=2)
        )
        assert update["verdict"] == "OK"
        assert update.get("forced") is True

    async def test_llm_failure_assumes_ok(self):
        class BrokenLLM:
            async def chat(self, *a, **k):
                raise RuntimeError("llm down")

        node = make_reflect(BrokenLLM(), AgentConfig(target="mock/model"))
        update = await node(make_state(row_count=2, columns=["x"], rows=[[1], [2]]))
        assert update["verdict"] == "OK"

    async def test_error_passthrough(self):
        node = self._make()
        update = await node(make_state(row_count=3, error="upstream failed"))
        assert update == {}


# ── Output ───────────────────────────────────────────────


class TestOutput:
    async def test_format_with_full_data(self):
        state = make_state(
            sql="SELECT county FROM students",
            columns=["county"],
            rows=[["Alameda"], ["Orange"]],
            row_count=2,
            execution_time_ms=15.0,
            verdict="OK",
        )
        update = await output(state)
        response = update["final_response"]
        assert "Question" in response
        assert "SELECT county" in response
        assert "Alameda" in response
        assert "2 rows" in response

    async def test_format_empty_result(self):
        update = await output(make_state(row_count=0))
        assert "zero rows" in update["final_response"]

    async def test_format_no_execution(self):
        """The 'empty' workflow has no execute_sql data (row_count stays -1)."""
        update = await output(make_state())
        assert "(No query executed)" in update["final_response"]

    async def test_format_limits_table_rows(self):
        rows = [[f"row{i}"] for i in range(30)]
        update = await output(make_state(columns=["col"], rows=rows, row_count=30))
        assert "10 more rows" in update["final_response"]

    async def test_error_state_formats_error_section(self):
        update = await output(make_state(error="SQL generation failed after 3 attempts"))
        response = update["final_response"]
        assert "**Error**" in response
        assert "3 attempts" in response
