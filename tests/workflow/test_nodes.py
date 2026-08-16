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

    async def test_rollback_rerun_keeps_previous_matches(self):
        """回退重跑 schema_linking 时,旧匹配表必须保留(并集),修漏表不丢旧表。"""
        class Catalog:
            async def search_tables(self, query, datasource=None, limit=10):
                return [{"name": "trans", "columns": 8, "row_count": 100}]

            async def list_tables(self, datasource=None):
                return [{"name": "trans", "columns": 8, "row_count": 100}]

            async def table_detail(self, name):
                return {
                    "name": name,
                    "columns": [{"name": "account_id", "type": "int"}],
                    "row_count": 100,
                }

        node = make_schema_linking(catalog=Catalog())
        update = await node(make_state(
            question="q",
            matched_tables=["account"],   # 上一轮已匹配的表
            error_feedback="missing trans table",  # 回退重跑信号
        ))
        assert "account" in update["matched_tables"]  # 旧匹配保留
        assert "trans" in update["matched_tables"]    # 新匹配并入

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

    async def test_zero_matches_falls_back_to_all_tables(self):
        """英文题复数/缩写匹配不到任何表时,兜底为全量表清单,保证 schema 锚定。"""
        class EmptySearchCatalog:
            async def search_tables(self, query, datasource=None, limit=10):
                return []

            async def list_tables(self, datasource=None):
                return [
                    {"name": "account", "columns": 4, "row_count": 4500},
                    {"name": "trans", "columns": 8, "row_count": 1056320},
                ]

            async def table_detail(self, name):
                return {
                    "name": name,
                    "columns": [{"name": "account_id", "type": "int"}],
                    "row_count": 100,
                }

        node = make_schema_linking(catalog=EmptySearchCatalog())
        update = await node(make_state(
            question="How many accounts choose issuance after transaction",
        ))
        assert "account" in update["matched_tables"]
        assert "trans" in update["matched_tables"]
        assert "account" in update["schema_context"]
        assert "No matching tables" not in update["schema_context"]


class TestPlannerRollbackRevision:
    async def test_rollback_revision_includes_prior_plan(self):
        """回退重跑规划时,上一版计划必须进 prompt(增量修订,非从零重写)。"""
        from trove.workflow.nodes.planner import make_planner

        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(messages=messages, **kwargs)
                return "new plan"

        node = make_planner(LLM(), AgentConfig(target="m"), agentic=False)
        state = make_state(
            plan="旧计划: join account 与 district 按地区聚合",
            error_feedback="wrong aggregation",
        )
        update = await node(state)
        assert update["plan"] == "new plan"
        prompt = " ".join(m["content"] for m in captured["messages"])
        assert "旧计划" in prompt          # 上一版计划原文
        assert "wrong aggregation" in prompt  # 失败原因


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

    async def test_enum_translations_reach_schema_context(
        self, catalog, sqlite_registry, kb_service, kb_ds_dir,
    ):
        """列枚举值说明（如 BIRD value_description）渲染进 schema 上下文。"""
        (kb_ds_dir / "schema_notes.yml").write_text(
            """
tables:
  - name: students
    description: 学生成绩表
    columns:
      - name: grade
        description: ""
        enums: ["'POPLATEK PO OBRATU' stands for issuance after transaction"]
""",
            encoding="utf-8",
        )
        node = make_schema_linking(
            catalog=catalog, kb=kb_service, connectors=sqlite_registry,
        )
        update = await node(make_state(question="学生们的平均成绩是多少"))
        assert "POPLATEK PO OBRATU" in update["schema_context"]
        assert "issuance after transaction" in update["schema_context"]

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


class TestCorrectionContextInjection:
    """回退重跑时把失败上下文带回上游步骤。"""

    async def test_schema_linking_search_includes_error_analysis(self, catalog):
        """schema_linking 重跑时，诊断文本进入目录搜索输入。"""
        class RecordingCatalog:
            def __init__(self):
                self.queries = []

            async def search_tables(self, query, limit=10):
                self.queries.append(query)
                return []

            async def table_detail(self, name):
                return None

        from trove.workflow.nodes.schema_linking import make_schema_linking

        rec = RecordingCatalog()
        node = make_schema_linking(catalog=rec, kb=None, connectors=None, fallback_all=False)
        await node(make_state(
            question="学生的平均成绩",
            error_analysis="判断: 漏了 nonexistent 表",
        ))
        assert rec.queries
        assert "nonexistent" in rec.queries[0]

    async def test_planner_prompt_includes_correction_context(self):
        """planner 重跑时，提示词携带上一次失败与诊断。"""
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured["prompt"] = " ".join(m["content"] for m in messages)
                return "plan: ok"

        from trove.core.config import AgentConfig
        from trove.workflow.nodes.planner import make_planner

        node = make_planner(LLM(), AgentConfig(target="m"), agentic=False)
        await node(make_state(
            error_feedback="no such table: loans",
            error_analysis="判断: loans 表不存在",
        ))
        assert "no such table" in captured["prompt"]
        assert "loans 表不存在" in captured["prompt"]


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

    def test_build_sql_prompt_renders_reasoning_context(self):
        prompt = build_sql_prompt(
            "q", "schema", "sqlite",
            reasoning_context="[gen_sql] 上一轮先试 validate_sql 再决定解释",
        )
        assert "validate_sql" in prompt

    def test_build_sql_prompt_evidence_sits_right_before_question(self):
        """证据提权：Evidence 区块紧贴 Question，是模型生成前最后读到的内容。"""
        prompt = build_sql_prompt(
            "q", "schema", "sqlite",
            evidence="Frequency = 'POPLATEK PO OBRATU' stands for issuance after transaction",
            few_shots=[{"question": "某示例", "sql": "SELECT 1", "template": False}],
        )
        assert "authoritative, must follow" in prompt
        assert "POPLATEK PO OBRATU" in prompt
        # 证据在示例之后、问题之前
        assert prompt.index("Reference examples") < prompt.index("Evidence")
        assert prompt.index("Evidence") < prompt.index("Question:")

    def test_build_sql_prompt_time_context_sits_before_question(self):
        """时间范围与证据同为权威块:Evidence 之后、Question 之前;未解析时不注入。"""
        prompt = build_sql_prompt(
            "q", "schema", "sqlite",
            evidence="official hint",
            time_context="2025-01-01 ~ 2025-01-15",
        )
        assert "Resolved time range" in prompt
        assert prompt.index("Evidence") < prompt.index("Resolved time range")
        assert prompt.index("Resolved time range") < prompt.index("Question:")

        assert "Resolved time range" not in build_sql_prompt("q", "schema", "sqlite")

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

    def test_build_sql_prompt_includes_plan(self):
        plan = "Join account with district; aggregate by district"
        prompt = build_sql_prompt("q", "schema", "sqlite", plan=plan)
        assert "Query plan" in prompt
        assert "Join account with district" in prompt

    def test_build_sql_prompt_includes_rules(self):
        rules = ["年龄 = 1998 - YEAR(birth_date)"]
        prompt = build_sql_prompt("q", "schema", "sqlite", rules=rules)
        assert "Data source rules" in prompt
        assert "1998 - YEAR(birth_date)" in prompt

    def test_build_sql_prompt_includes_lessons(self):
        lessons = [{"pattern": "loans", "note": "表名是 loan 不是 loans"}]
        prompt = build_sql_prompt("q", "schema", "sqlite", lessons=lessons)
        assert "Known pitfalls" in prompt
        assert "表名是 loan 不是 loans" in prompt

    def test_build_sql_prompt_without_plan_has_no_section(self):
        prompt = build_sql_prompt("q", "schema", "sqlite")
        assert "Query plan" not in prompt

    def test_build_fix_prompt_lists_errors(self):
        prompt = build_fix_prompt("SELEC 1", ["Parse error: bad"])
        assert "SELEC 1" in prompt
        assert "Parse error: bad" in prompt

    def test_build_fix_prompt_chinese(self):
        """中文修复提示词:保持原意图、只修语法(默认 en 的纯助手调用不受影响)。"""
        prompt = build_fix_prompt("SELEC 1", ["语法错误"], lang="zh")
        assert "SELEC 1" in prompt
        assert "语法错误" in prompt
        assert "保持原始查询意图" in prompt
        assert "只修正语法错误" in prompt
        # 默认仍是英文
        assert "failed validation" in build_fix_prompt("SELEC 1", ["e"])

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
        assert "校验错误" in llm.last_messages[-1]["content"]  # 默认 zh

    async def test_fix_prompt_follows_language(self):
        llm = ScriptedLLM(["```sql\nSELECT 1;\n```", "```sql\nSELECT 1;\n```"])
        generate = make_generate(llm, self._config())
        from trove.workflow.state import GenSQLState
        state = GenSQLState(
            question="q", schema_context="", dialect="sqlite", lang="en",
            sql="SELEC 1", attempts=1, validation_errors=["Parse error"],
        )
        await generate(state)
        assert "failed validation" in llm.last_messages[-1]["content"]

    async def test_skips_when_error_present(self):
        class RaisingLLM:
            async def chat(self, *a, **k):
                raise AssertionError("LLM must not be called")

        generate = make_generate(RaisingLLM(), self._config())
        from trove.workflow.state import GenSQLState
        state = GenSQLState(question="q", schema_context="", dialect="sqlite", error="upstream")
        assert await generate(state) == {}

    async def test_passes_trace_metadata(self):
        """gen_sql 的 trace metadata 含 node/session。"""
        from trove.workflow.state import GenSQLState

        captured = {}

        class CapturingLLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(kwargs)
                return "```sql\nSELECT 1;\n```"

        generate = make_generate(CapturingLLM(), self._config())
        state = GenSQLState(
            question="q", schema_context="", dialect="sqlite", session_id="s9",
        )
        await generate(state)
        assert captured["metadata"]["node"] == "gen_sql"
        assert captured["metadata"]["session_id"] == "s9"

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

    async def test_execute_records_tool_span(self, sqlite_registry, monkeypatch):
        """SQL 执行作为 tool span 记录（轨迹里的工具调用可见）。"""
        import trove.workflow.nodes.execute_sql as execute_module

        spans = []

        class FakeSpan:
            def __init__(self):
                self.output = None

            def update(self, **kwargs):
                self.output = kwargs

        class FakeCM:
            def __init__(self, name, input):
                spans.append({"name": name, "input": input, "span": FakeSpan()})

            def __enter__(self):
                return spans[-1]["span"]

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            execute_module, "record_span",
            lambda name, input=None: FakeCM(name, input),
        )
        node = make_execute_sql(sqlite_registry)
        await node(make_state(sql="SELECT name FROM students ORDER BY name"))

        assert spans[0]["name"] == "tool.execute_sql"
        assert "SELECT name" in spans[0]["input"]
        assert spans[0]["span"].output["output"]["row_count"] == 5

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
        update = await node(make_state(sql="SELECT * FROM nonexistent", retry_count=10))
        assert update["error"]
        assert "error_feedback" not in update

    async def test_error_passthrough(self, sqlite_registry):
        node = make_execute_sql(sqlite_registry)
        update = await node(make_state(sql="SELECT 1", error="upstream failed"))
        assert update == {}


# ── Planner ──────────────────────────────────────────────


class TestBilingualPrompts:
    async def test_gen_system_prompt_follows_language(self):
        """中文问题 → 中文 system prompt；英文问题 → 英文。"""
        from trove.workflow.state import GenSQLState
        from trove.workflow.nodes.gen_sql import make_generate

        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(messages=messages)
                return "```sql\nSELECT 1;\n```"

        generate = make_generate(LLM(), AgentConfig(target="m"))
        await generate(GenSQLState(question="平均成绩是多少", schema_context="", dialect="sqlite"))
        assert "生成" in captured["messages"][0]["content"]  # 默认中文 system

        # 语言跟随 state.lang(配置),不按问题语言检测
        await generate(GenSQLState(question="average grade", schema_context="", dialect="sqlite", lang="en"))
        assert "SQL generation assistant" in captured["messages"][0]["content"]

    async def test_planner_prompt_follows_language(self):
        from trove.workflow.nodes.planner import make_planner

        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(messages=messages)
                return "plan"

        node = make_planner(LLM(), AgentConfig(target="m"))
        await node(make_state(question="平均成绩是多少"))
        assert "规划" in captured["messages"][0]["content"]

        # 语言跟随 state.lang(配置),不按问题语言检测
        await node(make_state(question="average grade", lang="en"))
        assert "query planner" in captured["messages"][0]["content"]

    async def test_reflect_prompt_follows_language(self):
        from trove.workflow.nodes.reflect import make_reflect

        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(messages=messages)
                return "OK"

        node = make_reflect(LLM(), AgentConfig(target="m"))
        await node(make_state(question="平均成绩是多少", row_count=3, columns=["x"], rows=[[1], [2], [3]]))
        assert "评估" in captured["messages"][0]["content"]

    async def test_reflect_prompt_has_checkpoints_and_guardrails(self):
        """reflect 细化:列冗余/缺失、列顺序检查点 + 决策护栏(双语)。"""
        from trove.workflow.nodes.reflect import make_reflect

        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(messages=messages)
                return "OK"

        node = make_reflect(LLM(), AgentConfig(target="m"))
        await node(make_state(question="平均成绩是多少", row_count=3, columns=["x"], rows=[[1], [2], [3]]))
        zh_system = captured["messages"][0]["content"]
        assert "列是否冗余或缺失" in zh_system
        assert "列顺序是否符合问题要求" in zh_system
        assert "过滤条件过严" in zh_system

        await node(make_state(question="average grade", row_count=3, columns=["x"], rows=[[1], [2], [3]], lang="en"))
        en_system = captured["messages"][0]["content"]
        assert "redundant or missing" in en_system
        assert "over-restrictive" in en_system
        assert "extra cautious" in en_system

    async def test_reflect_empty_verdict_treated_as_retry(self):
        """空输出/不可解析裁决不得静默放行——语义检查没有发生,按 RETRY 修正。"""
        from trove.workflow.nodes.reflect import make_reflect

        class EmptyLLM:
            async def chat(self, model, messages, **kwargs):
                return ""

        node = make_reflect(EmptyLLM(), AgentConfig(target="m"))
        update = await node(make_state(
            question="what is the increase rate", row_count=1,
            columns=["a", "b", "c"], rows=[[1, 2, 3]],
        ))
        assert update["verdict"] == "RETRY"
        assert "unparseable" in update["reason"]
        assert update["retry_count"] == 1

    async def test_reflect_unparseable_verdict_forced_ok_at_cap(self):
        """空裁决连续打回达语义上限 → 强制接受,保证收敛。"""
        from trove.workflow.nodes.reflect import make_reflect

        class EmptyLLM:
            async def chat(self, model, messages, **kwargs):
                return "  \n"

        node = make_reflect(EmptyLLM(), AgentConfig(target="m"))
        update = await node(make_state(
            question="what is the increase rate", row_count=1,
            columns=["x"], rows=[[1]], semantic_retries=2,
        ))
        assert update["verdict"] == "OK"
        assert update["forced"] is True


class TestPlanner:
    async def test_planner_writes_plan(self):
        from trove.workflow.nodes.planner import make_planner

        llm = ScriptedLLM(["Use students, aggregate grade by county."])
        node = make_planner(llm, AgentConfig(target="mock/model"))
        update = await node(make_state(schema_context="Table: students"))
        assert "students" in update["plan"]
        # planner prompt 带 schema 与问题
        prompt_text = " ".join(m["content"] for m in llm.last_messages)
        assert "students" in prompt_text

    async def test_planner_llm_failure_is_silent(self):
        from trove.workflow.nodes.planner import make_planner

        class BrokenLLM:
            async def chat(self, *a, **k):
                raise RuntimeError("llm down")

        node = make_planner(BrokenLLM(), AgentConfig(target="mock/model"))
        assert await node(make_state()) == {}

    async def test_planner_error_passthrough(self):
        from trove.workflow.nodes.planner import make_planner

        node = make_planner(ScriptedLLM(["x"]), AgentConfig(target="mock/model"))
        assert await node(make_state(error="upstream")) == {}

    async def test_planner_carries_llm_detail(self):
        from trove.workflow.nodes.planner import make_planner

        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "plan text"

        node = make_planner(LLM(), AgentConfig(target="mock/model"))
        update = await node(make_state())
        assert update["llm"]["model"] == "mock/model"
        assert update["llm"]["output_preview"] == "plan text"

    async def test_planner_passes_trace_metadata(self):
        """trace metadata（node/session/question）随 LLM 调用上报。"""
        from trove.workflow.nodes.planner import make_planner

        captured = {}

        class CapturingLLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(kwargs)
                return "plan text"

        node = make_planner(CapturingLLM(), AgentConfig(target="mock/model"))
        await node(make_state(question="average grade by county"))
        assert captured["metadata"]["node"] == "planner"
        assert captured["metadata"]["session_id"] == "s1"

    async def test_planner_sees_resolved_time_range(self):
        """planner 起草过滤条件时能看到解析出的时间范围;未解析时不注入。"""
        from trove.workflow.nodes.planner import make_planner

        captured = {}

        class CapturingLLM:
            async def chat(self, model, messages, **kwargs):
                captured["user"] = messages[1]["content"]
                return "plan text"

        node = make_planner(CapturingLLM(), AgentConfig(target="mock/model"))
        await node(make_state(time_context="2025-01-01 ~ 2025-01-15"))
        assert "Resolved time range: 2025-01-01 ~ 2025-01-15" in captured["user"]

        await node(make_state())
        assert "Resolved time range" not in captured["user"]


# ── Clarify ──────────────────────────────────────────────


class TestClarify:
    async def test_matched_tables_pass(self):
        from trove.workflow.nodes.clarify import make_clarify

        node = make_clarify()
        update = await node(make_state(matched_tables=["students"]))
        assert update == {}

    async def test_no_tables_sets_clarification(self):
        """无表匹配 → 反问用户，而不是生成 SQL。"""
        from trove.workflow.nodes.clarify import make_clarify

        node = make_clarify()
        update = await node(make_state(matched_tables=[], question="那个数据是多少"))
        assert update["clarification_question"]
        assert "匹配" in update["clarification_question"]

    async def test_error_passthrough(self):
        from trove.workflow.nodes.clarify import make_clarify

        node = make_clarify()
        update = await node(make_state(matched_tables=[], error="upstream"))
        assert update == {}


# ── Validate rules ───────────────────────────────────────


class TestValidateRules:
    async def test_rule_failure_gives_feedback(self):
        """count 问题返回多行 → 确定性规则失败 → 反馈修正。"""
        from trove.workflow.nodes.validate import make_validate_rules

        node = make_validate_rules()
        state = make_state(
            question="how many students",
            sql="SELECT name FROM students",
            columns=["name"],
            rows=[["a"], ["b"]],
            row_count=2,
        )
        update = await node(state)
        assert "error" not in update
        assert "计数问题应返回单个数字" in update["error_feedback"]  # 默认中文
        assert update["retry_count"] == 1

    async def test_rule_failure_budget_exhausted_degrades(self):
        from trove.workflow.nodes.validate import make_validate_rules

        node = make_validate_rules()
        state = make_state(
            question="how many students",
            sql="SELECT name FROM students",
            columns=["name"],
            rows=[["a"], ["b"]],
            row_count=2,
            retry_count=10,
        )
        update = await node(state)
        assert update["error"]

    async def test_pass_returns_empty_update(self):
        from trove.workflow.nodes.validate import make_validate_rules

        node = make_validate_rules()
        update = await node(make_state(question="average grade", row_count=0))
        assert update == {}

    async def test_pending_feedback_passes_through(self):
        """execute 已挂起反馈时，校验节点不覆盖。"""
        from trove.workflow.nodes.validate import make_validate_rules

        node = make_validate_rules()
        state = make_state(
            question="how many students",
            error_feedback="pending execution error",
            row_count=-1,
        )
        assert await node(state) == {}


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

    async def test_prompt_includes_schema_context(self):
        """裁决 prompt 带 schema 上下文，模型不必用工具去猜表结构。"""
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(messages=messages)
                return "OK"

        node = make_reflect(LLM(), AgentConfig(target="m"))
        await node(make_state(
            row_count=2, columns=["name"], rows=[["a"], ["b"]],
            schema_context="Table: students(id, name)",
        ))
        user_msg = captured["messages"][1]["content"]
        assert "students(id, name)" in user_msg

    async def test_single_shot_judge_without_tools(self):
        """裁决是单次 LLM 判断：不调 chat_full、不给工具（确定性规则已在前置拦截）。"""
        class OnlyChatLLM:
            def __init__(self):
                self.calls = 0

            async def chat(self, model, messages, **kwargs):
                self.calls += 1
                return "OK"

            async def chat_full(self, *a, **k):
                raise AssertionError("single-shot judge must not use chat_full")

        llm = OnlyChatLLM()
        node = make_reflect(llm, AgentConfig(target="m"))
        update = await node(make_state(row_count=2, columns=["name"], rows=[["a"], ["b"]]))
        assert update["verdict"] == "OK"
        assert llm.calls == 1

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
            make_state(row_count=3, columns=["x"], rows=[[1], [2], [3]], retry_count=10)
        )
        assert update["verdict"] == "OK"
        assert update.get("forced") is True

    async def test_no_sql_verdict(self):
        """NO_SQL 裁决：不是 SQL 问题 → 置 no_sql 标志，不消耗重试预算。"""
        node = self._make("NO_SQL: 这是表含义问题，不是数据查询")
        update = await node(make_state(row_count=3, columns=["x"], rows=[[1], [2], [3]]))
        assert update["verdict"] == "NO_SQL"
        assert update["reason"] == "这是表含义问题，不是数据查询"
        assert update["no_sql"] is True
        assert "retry_count" not in update  # 不是重试，不消耗共享修正预算

    async def test_no_sql_not_forced_at_retry_cap(self):
        """重试上限处 NO_SQL 裁决不被强制为 OK（大小写前缀均可解析）。"""
        node = self._make("no_sql: definitional question")
        update = await node(
            make_state(row_count=3, columns=["x"], rows=[[1], [2], [3]], retry_count=10)
        )
        assert update["verdict"] == "NO_SQL"
        assert update["reason"] == "definitional question"
        assert update.get("forced") is None

    async def test_empty_result_with_weak_signal_still_judges(self):
        """0 行 + 元数据倾向问题 → 不短路，仍由 LLM 裁决（可给出 NO_SQL）。"""
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "EMPTY"

        node = make_reflect(LLM(), AgentConfig(target="m"))
        update = await node(make_state(row_count=0, question="disp 表是啥"))
        assert update["verdict"] == "EMPTY"  # 来自 LLM 裁决，而非 0 行短路

    async def test_prompt_includes_sql_and_evidence(self):
        """裁决 prompt 带 SQL 与官方证据：judge 能对照语义发现实现漂移。"""
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured["user"] = messages[1]["content"]
                return "RETRY: SQL 用日期比较偷换了枚举过滤语义"

        node = make_reflect(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            row_count=1, columns=["n"], rows=[[0]],
            sql="SELECT COUNT(DISTINCT a.account_id) FROM account a "
                "JOIN trans t ON a.account_id = t.account_id WHERE l.date > t.date",
            evidence="Frequency = 'POPLATEK PO OBRATU' stands for issuance after transaction",
        ))
        assert update["verdict"] == "RETRY"
        assert "l.date > t.date" in captured["user"]
        assert "POPLATEK PO OBRATU" in captured["user"]

    async def test_prompt_includes_resolved_time_range(self):
        """评审输入带解析出的时间范围,便于核对 SQL 的时间过滤;无解析结果时不注入。"""
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured["user"] = messages[1]["content"]
                return "OK"

        node = make_reflect(LLM(), AgentConfig(target="m"))
        await node(make_state(
            row_count=1, columns=["n"], rows=[[0]],
            time_context="2025-01-01 ~ 2025-01-15",
        ))
        assert "Resolved time range" in captured["user"]
        assert "2025-01-01 ~ 2025-01-15" in captured["user"]

        await node(make_state(row_count=1, columns=["n"], rows=[[0]]))
        assert "Resolved time range" not in captured["user"]

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

    async def test_semantic_retry_cap_forces_ok(self):
        """连续纯语义 RETRY(执行成功、无执行错误)达到上限 → 强制接受,
        防止欠定问题被法官无限重审烧光预算。"""
        node = make_reflect(LLMGateway(mock_response="RETRY: gap semantics"), AgentConfig(target="m"))
        state = make_state(row_count=1, columns=["x"], rows=[[1]])
        for _ in range(2):
            update = await node(state)
            assert update["verdict"] == "RETRY"
            state = make_state(**{**state.model_dump(), **update})
        update = await node(state)  # 第 3 次连续语义 RETRY(达到上限) → forced OK
        assert update["verdict"] == "OK"
        assert update["forced"] is True

    async def test_execution_error_resets_semantic_counter(self):
        """执行错误后的 RETRY 是修执行问题,不算语义重审,计数器归零。"""
        node = make_reflect(LLMGateway(mock_response="RETRY: fix it"), AgentConfig(target="m"))
        state = make_state(row_count=1, columns=["x"], rows=[[1]])
        u1 = await node(state)
        assert u1["semantic_retries"] == 1
        state = make_state(**{**state.model_dump(), **u1})
        state = make_state(**{**state.model_dump(), "error_feedback": "SQL execution error"})
        u2 = await node(state)
        assert u2["semantic_retries"] == 0

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

    async def test_low_confidence_rendered(self):
        """多候选不一致耗尽 → 输出主候选 + 低置信标注。"""
        state = make_state(row_count=0, consensus=False)
        response = (await output(state))["final_response"]
        assert "Confidence" in response
        assert "low" in response.lower()

    async def test_high_confidence_no_note(self):
        response = (await output(make_state(row_count=0)))["final_response"]
        assert "Confidence" not in response

    async def test_clarification_rendered(self):
        """需要澄清时输出反问，而非答案。"""
        state = make_state(clarification_question="请说明你想查询哪张表的数据")
        response = (await output(state))["final_response"]
        assert "Clarification" in response
        assert "请说明你想查询哪张表的数据" in response


class TestSemanticPromptGuards:
    """① planner 作用域原则 + ③ reflect 条件完整性检查(冷启动语义防线)。"""

    def test_planner_prompt_carries_scope_principle(self):
        from trove.workflow.nodes.planner import (
            PLANNER_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT_ZH,
        )
        assert "lowest" in PLANNER_SYSTEM_PROMPT.lower()
        assert "scope" in PLANNER_SYSTEM_PROMPT.lower()
        assert "作用域" in PLANNER_SYSTEM_PROMPT_ZH
        assert "最低" in PLANNER_SYSTEM_PROMPT_ZH

    def test_reflect_prompt_carries_condition_completeness(self):
        from trove.workflow.nodes.reflect import (
            REFLECT_SYSTEM_PROMPT, REFLECT_SYSTEM_PROMPT_ZH,
        )
        assert "every condition" in REFLECT_SYSTEM_PROMPT
        assert "每个条件" in REFLECT_SYSTEM_PROMPT_ZH

    def test_reflect_prompt_guards_against_rearguing_ambiguity(self):
        """法官不得重新争论问题歧义,也不得用'并列可能漏行'打回 LIMIT 1。"""
        from trove.workflow.nodes.reflect import (
            REFLECT_SYSTEM_PROMPT, REFLECT_SYSTEM_PROMPT_ZH,
        )
        assert "interpretation" in REFLECT_SYSTEM_PROMPT
        assert "LIMIT 1" in REFLECT_SYSTEM_PROMPT
        assert "合理解读" in REFLECT_SYSTEM_PROMPT_ZH
        assert "并列" in REFLECT_SYSTEM_PROMPT_ZH
        assert "formula" in REFLECT_SYSTEM_PROMPT
        assert "公式" in REFLECT_SYSTEM_PROMPT_ZH


class TestStructuredPlan:
    """⑦ planner 结构化输出:JSON 计划 → 渲染进 gen_sql 提示词,解析失败回退散文。"""

    JSON_PLAN = """
{
  "tables": ["loan", "account"],
  "joins": "loan.account_id = account.account_id",
  "conditions": [
    {"field": "loan.date", "op": "=", "value": "1997", "note": "贷款批准年份"},
    {"field": "account.frequency", "op": "=", "value": "POPLATEK TYDNE", "note": "周发放"}
  ],
  "aggregation": "none",
  "extreme": {"func": "min", "column": "loan.amount", "scope": "after all filters"},
  "ordering": "amount asc",
  "answer_columns": ["account_id"]
}
"""

    def test_parse_plain_json(self):
        from trove.workflow.nodes.planner import _parse_plan
        data = _parse_plan(self.JSON_PLAN)
        assert data["extreme"]["scope"] == "after all filters"
        assert len(data["conditions"]) == 2

    def test_parse_fenced_json(self):
        from trove.workflow.nodes.planner import _parse_plan
        data = _parse_plan(f"```json\n{self.JSON_PLAN}\n```")
        assert data["answer_columns"] == ["account_id"]

    def test_parse_prose_returns_none(self):
        from trove.workflow.nodes.planner import _parse_plan
        assert _parse_plan("先取1997年贷款，再筛选周发放的账户") is None
        assert _parse_plan("plan: ok") is None

    def test_render_makes_scope_explicit(self):
        from trove.workflow.nodes.planner import _parse_plan, _render_plan
        text = _render_plan(_parse_plan(self.JSON_PLAN), lang="zh")
        assert "贷款批准年份" in text          # 条件带注释
        assert "周发放" in text
        assert "after all filters" in text     # 作用域显式
        assert "account_id" in text

    async def test_node_renders_structured_plan(self):
        from trove.core.config import AgentConfig
        from trove.workflow.nodes.planner import make_planner

        class LLM:
            async def chat(self, model, messages, **kwargs):
                return TestStructuredPlan.JSON_PLAN

        node = make_planner(LLM(), AgentConfig(target="m"), agentic=False)
        update = await node(make_state())
        assert "Conditions" in update["plan"] or "条件" in update["plan"]
        assert "after all filters" in update["plan"]

    async def test_node_falls_back_to_prose(self):
        """模型不听话输出散文时:计划原样保留,管线不因结构化失败而中断。"""
        from trove.core.config import AgentConfig
        from trove.workflow.nodes.planner import make_planner

        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "先筛选1997年贷款，再取最低金额。"

        node = make_planner(LLM(), AgentConfig(target="m"), agentic=False)
        update = await node(make_state())
        assert update["plan"] == "先筛选1997年贷款，再取最低金额。"
