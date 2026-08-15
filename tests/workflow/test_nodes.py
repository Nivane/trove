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


@pytest.fixture
def kb_service(tmp_path, sqlite_registry):
    """KbService with an existing kb directory; datasource = sqlite_registry name."""
    from trove.services.kb.service import KbService

    kb = KbService(tmp_path / "proj")
    (kb.kb_dir / sqlite_registry.default_name).mkdir(parents=True)
    return kb


@pytest.fixture
def kb_ds_dir(kb_service, sqlite_registry):
    return kb_service.kb_dir / sqlite_registry.default_name


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


class TestSchemaLinkingWithKB:
    def _kb_with_terms(self, kb_service, kb_ds_dir):
        (kb_ds_dir / "semantics.yml").write_text(
            """
terms:
  - term: 平均成绩
    aliases: [平均分]
    mapping: AVG(students.grade)
    tables: [students]
    definition: 学生平均分
""",
            encoding="utf-8",
        )
        return kb_service

    async def test_chinese_question_matches_via_terms(
        self, catalog, sqlite_registry, kb_service, kb_ds_dir,
    ):
        """中文问题无 ASCII 分词，靠语义术语命中表（中文匹配修复）。"""
        node = make_schema_linking(
            catalog=catalog,
            kb=self._kb_with_terms(kb_service, kb_ds_dir),
            connectors=sqlite_registry,
        )
        update = await node(make_state(question="学生们的平均成绩是多少"))
        assert "students" in update["matched_tables"]
        assert any(h["term"] == "平均成绩" for h in update["kb_hits"])

    async def test_notes_included_in_schema_context(
        self, catalog, sqlite_registry, kb_service, kb_ds_dir,
    ):
        self._kb_with_terms(kb_service, kb_ds_dir)
        (kb_ds_dir / "schema_notes.yml").write_text(
            """
tables:
  - name: students
    description: 学生成绩表
    columns:
      - name: grade
        description: 考试成绩
    metrics:
      - name: avg_grade
        definition: 平均成绩
""",
            encoding="utf-8",
        )
        node = make_schema_linking(
            catalog=catalog, kb=kb_service, connectors=sqlite_registry,
        )
        update = await node(make_state(question="学生们的平均成绩是多少"))
        assert "学生成绩表" in update["schema_context"]
        assert "考试成绩" in update["schema_context"]

    async def test_other_datasource_kb_not_visible(
        self, catalog, sqlite_registry, kb_service, kb_ds_dir,
    ):
        """知识按数据源隔离：另一个数据源目录的术语不可见。"""
        self._kb_with_terms(kb_service, kb_ds_dir)
        other = kb_service.kb_dir / "other_db"
        other.mkdir()
        (other / "semantics.yml").write_text(
            "terms:\n  - term: 别的术语\n    tables: [ghost]\n",
            encoding="utf-8",
        )
        node = make_schema_linking(
            catalog=catalog, kb=kb_service, connectors=sqlite_registry,
        )
        update = await node(make_state(question="别的术语查询"))
        assert "ghost" not in update["matched_tables"]

    async def test_no_kb_unchanged(self, catalog):
        """kb=None 时行为与现状一致（无 kb_hits 键）。"""
        node = make_schema_linking(catalog=catalog, kb=None)
        update = await node(make_state())
        assert "kb_hits" not in update

    async def test_join_hints_in_schema_context(self, catalog, sqlite_registry):
        """*_id 列名启发式：schema_context 带 Join 路径提示。"""
        adapter = await sqlite_registry.get()
        await adapter.execute(
            "CREATE TABLE district (district_id INTEGER PRIMARY KEY, name TEXT)"
        )
        await adapter.execute(
            "CREATE TABLE city (city_id INTEGER PRIMARY KEY, district_id INTEGER)"
        )
        node = make_schema_linking(catalog=catalog, connectors=sqlite_registry)
        update = await node(make_state(question="city and district info"))
        assert "city.district_id → district.district_id" in update["schema_context"]


# ── Value linking ────────────────────────────────────────


class TestValueCandidates:
    def test_quoted_and_capitalized_words(self):
        from trove.workflow.nodes.schema_linking import _extract_value_candidates

        q = "账户 'POPLATEK' 在 Benesov 地区的贷款"
        assert "POPLATEK" in _extract_value_candidates(q)
        assert "Benesov" in _extract_value_candidates(q)

    def test_no_candidates_for_plain_question(self):
        from trove.workflow.nodes.schema_linking import _extract_value_candidates

        assert _extract_value_candidates("哪个地区的平均贷款金额最高?") == []

    def test_candidates_deduplicated_and_limited(self):
        from trove.workflow.nodes.schema_linking import _extract_value_candidates

        q = "'XX' 'XX' " + " ".join(f"Word{i}" for i in range(8))
        result = _extract_value_candidates(q)
        assert result.count("XX") == 1
        assert len(result) <= 5


class TestValueLinkingInSchemaContext:
    async def test_value_hints_for_matched_tables(self, catalog, sqlite_registry):
        """问题中的实体在匹配表的列值中命中 → schema_context 带 Value hints。"""
        node = make_schema_linking(catalog=catalog, connectors=sqlite_registry)
        update = await node(make_state(question="Alameda 县学生的平均 grade 成绩"))
        assert "Value hints" in update["schema_context"]
        assert "Alameda" in update["schema_context"]
        assert "students.county" in update["schema_context"]

    async def test_no_value_hints_without_hits(self, catalog, sqlite_registry):
        node = make_schema_linking(catalog=catalog, connectors=sqlite_registry)
        update = await node(make_state(question="学生的平均成绩"))
        assert "Value hints" not in update["schema_context"]


# ── Join hints ───────────────────────────────────────────


class TestJoinHints:
    def test_infer_from_id_suffix(self):
        """account.district_id 且存在 district 表 → 生成 Join 提示。"""
        from trove.workflow.nodes.schema_linking import _join_hints

        hints = _join_hints(
            "account", ["account_id", "district_id", "frequency"],
            {"district": ["district_id", "A2"]},
        )
        assert hints == ["account.district_id → district.district_id"]

    def test_target_column_falls_back_to_id(self):
        from trove.workflow.nodes.schema_linking import _join_hints

        hints = _join_hints(
            "loan", ["loan_id", "account_id"],
            {"account": ["account_id"]},
        )
        assert hints == ["loan.account_id → account.account_id"]

    def test_no_matching_table_no_hint(self):
        from trove.workflow.nodes.schema_linking import _join_hints

        assert _join_hints(
            "account", ["account_id", "frequency"], {"district": ["district_id"]},
        ) == []

    def test_self_and_unknown_targets_skipped(self):
        from trove.workflow.nodes.schema_linking import _join_hints

        # 无对应表 / 无对应列 → 不产生提示
        assert _join_hints("account", ["ghost_id"], {"account": ["account_id"]}) == []
        assert _join_hints("account", ["account_id"], {"account": ["account_id"]}) == []


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

    def test_build_sql_prompt_includes_few_shots_and_terms(self):
        few_shots = [{"question": "各地区平均成绩", "sql": "SELECT 1", "template": False}]
        term_notes = [{"term": "平均成绩", "mapping": "AVG(students.grade)", "definition": "学生平均分"}]
        prompt = build_sql_prompt("q", "schema", "sqlite", few_shots=few_shots, term_notes=term_notes)
        assert "Reference examples" in prompt
        assert "SELECT 1" in prompt
        assert "AVG(students.grade)" in prompt

    def test_build_sql_prompt_without_kb_has_no_sections(self):
        prompt = build_sql_prompt("q", "schema", "sqlite")
        assert "Reference examples" not in prompt
        assert "Terminology" not in prompt

    def test_build_sql_prompt_includes_error_feedback(self):
        prompt = build_sql_prompt("q", "schema", "sqlite", error_feedback="no such table: loans")
        assert "no such table: loans" in prompt
        assert "failed during execution" in prompt

    def test_build_sql_prompt_includes_history(self):
        history = "user: 平均成绩是多少\nassistant: 平均成绩是 85 分"
        prompt = build_sql_prompt("q", "schema", "sqlite", history=history)
        assert "Conversation history" in prompt
        assert "平均成绩是 85 分" in prompt

    def test_build_sql_prompt_without_history_has_no_section(self):
        prompt = build_sql_prompt("q", "schema", "sqlite")
        assert "Conversation history" not in prompt

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
        assert update["error_feedback"] == ""  # 成功执行清空反馈

    async def test_execute_error_gives_feedback_for_retry(self, sqlite_registry):
        """第一次执行失败 → 反馈修正（不降级），消耗一轮修正预算。"""
        node = make_execute_sql(sqlite_registry)
        update = await node(make_state(sql="SELECT * FROM nonexistent"))
        assert "error" not in update
        assert "nonexistent" in update["error_feedback"]
        assert update["retry_count"] == 1

    async def test_execute_error_with_budget_exhausted_degrades(self, sqlite_registry):
        """修正预算耗尽后，执行失败才优雅降级。"""
        node = make_execute_sql(sqlite_registry)
        update = await node(make_state(sql="SELECT * FROM nonexistent", retry_count=2))
        assert update["error"]
        assert "error_feedback" not in update

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

    async def test_kb_hits_rendered(self):
        state = make_state(
            row_count=0,
            kb_hits=[
                {"kind": "term", "term": "平均成绩", "mapping": "AVG(students.grade)"},
                {"kind": "example", "question": "各地区平均成绩"},
            ],
        )
        response = (await output(state))["final_response"]
        assert "Knowledge base" in response
        assert "平均成绩 → AVG(students.grade)" in response
        assert "1 example used" in response

    async def test_no_kb_hits_no_kb_line(self):
        response = (await output(make_state(row_count=0)))["final_response"]
        assert "Knowledge base" not in response
