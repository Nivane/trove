"""Workflow node function tests (LangGraph era).

Nodes are plain async functions `async def node(state) -> dict` that
return a partial state update. Services are bound at construction time
via factory functions.
"""

import json

import pytest

from trove.core.config import AgentConfig
from trove.workflow.state import WorkflowState
from trove.llm.gateway import LLMGateway

from trove.workflow.nodes.schema_linking import make_schema_linking
from trove.workflow.nodes.gen_sql import (
    _like_pattern,
    build_fix_prompt,
    build_sql_prompt,
    build_sql_prompt_from_state,
    extract_sql,
    make_generate,
    make_sql_tools,
    make_validate,
    render_cache_prefix,
    search_values,
    static_semantic_warnings,
    validate_sql,
)
from trove.prompts import render
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


class TestSchemaLinkingSemanticLayer:
    """实时语义层(OSSIE provider)渲染进 schema_context。"""

    class FakeProvider:
        enabled = True

        def __init__(self, metrics, instructions=""):
            self._metrics = metrics
            self._instructions = instructions

        def metrics(self):
            return list(self._metrics)

        @property
        def instructions(self):
            return self._instructions

    @pytest.fixture
    def semantic_metrics(self):
        from trove.services.semantic_layer.models import SemanticMetric
        return [
            SemanticMetric(
                name="total_loan_amount", expression="SUM(loan.amount)",
                datasets=["loan"], definition="Total amount of all loans"),
            SemanticMetric(
                name="ghost_metric", expression="SUM(ghost.col)",
                datasets=["ghost"], definition="Ghost"),
            SemanticMetric(name="global_count", expression="COUNT(*)"),
        ]

    async def test_renders_anchored_metrics_and_instructions(self, demo_registry, semantic_metrics):
        from trove.services.datasource.catalog import CatalogService
        node = make_schema_linking(
            catalog=CatalogService(demo_registry),
            connectors=demo_registry,
            semantic_layer=self.FakeProvider(
                semantic_metrics, instructions="Use this model for loan analysis"),
        )
        update = await node(make_state(question="What is the total loan amount?"))
        ctx = update["schema_context"]
        # 锚定命中 loan 表 → 进该表段
        assert "Semantic metrics:" in ctx
        assert "total_loan_amount: SUM(loan.amount) — Total amount of all loans" in ctx
        # 模型级 AI 使用说明
        assert "Semantic note: Use this model for loan analysis" in ctx
        # 无数据集锚定 → 模型级块
        assert "global_count: COUNT(*)" in ctx
        # 数据集没进 matched_tables → 不渲染
        assert "ghost_metric" not in ctx

    async def test_disabled_provider_renders_nothing(self, demo_registry, semantic_metrics):
        from trove.services.datasource.catalog import CatalogService
        provider = self.FakeProvider(semantic_metrics, instructions="note")
        provider.enabled = False
        node = make_schema_linking(
            catalog=CatalogService(demo_registry),
            connectors=demo_registry,
            semantic_layer=provider,
        )
        update = await node(make_state(question="What is the total loan amount?"))
        assert "Semantic metrics:" not in update["schema_context"]
        assert "Semantic note:" not in update["schema_context"]


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
        from tests.helpers.kb import ossie_semantics_yaml

        (kb_ds_dir / "semantics.yml").write_text(ossie_semantics_yaml([
            {"term": "平均成绩", "aliases": ["平均分"], "mapping": "AVG(students.grade)",
             "tables": ["students"], "definition": "学生平均分"},
        ]))
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

    async def test_stats_lines_reach_schema_context(
        self, catalog, sqlite_registry, kb_service, kb_ds_dir,
    ):
        """profiling stats 渲染进 schema_context(仅异常证据,平凡统计不显示)。"""
        (kb_ds_dir / "schema_notes.yml").write_text(
            """
tables:
  - name: students
    description: 学生表
    row_count: 5
    columns:
      - name: grade
        description: 成绩
        stats:
          null_ratio: 0.0
          distinct: 5
          min: 75
          max: 99
      - name: note
        description: 备注
        stats:
          null_ratio: 0.9
          shape: json
""",
            encoding="utf-8",
        )
        node = make_schema_linking(
            catalog=catalog, kb=kb_service, connectors=sqlite_registry,
        )
        update = await node(make_state(question="students"))
        assert "Stats:" in update["schema_context"]
        assert "note: 90% NULL, shape=json" in update["schema_context"]
        assert "grade: range 75 .. 99" in update["schema_context"]
        # 平凡统计(0% NULL / 高 distinct)不渲染
        assert "grade: 0% NULL" not in update["schema_context"]
        assert "grade: 5 distinct" not in update["schema_context"]

    async def test_top_values_reach_schema_context(
        self, catalog, sqlite_registry, kb_service, kb_ds_dir,
    ):
        """P1-4:Top-K 值渲染进 Stats 段(规范拼写/脏值就地可见)。"""
        (kb_ds_dir / "schema_notes.yml").write_text(
            """
tables:
  - name: students
    description: 学生表
    row_count: 5
    columns:
      - name: county
        description: 县
        stats:
          distinct: 3
          top_values:
            - ["Alameda", 2]
            - ["Orange", 2]
            - ["Los Angeles", 1]
""",
            encoding="utf-8",
        )
        node = make_schema_linking(
            catalog=catalog, kb=kb_service, connectors=sqlite_registry,
        )
        update = await node(make_state(question="students"))
        assert "top values: Alameda (2), Orange (2), Los Angeles (1)" in update["schema_context"]


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
        from tests.helpers.kb import ossie_semantics_yaml

        other = kb_service.kb_dir / "other_db"
        other.mkdir()
        (other / "semantics.yml").write_text(ossie_semantics_yaml([
            {"term": "别的术语", "mapping": "COUNT(ghost.id)", "tables": ["ghost"]},
        ]))
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
        """P0-3:*_id 命名启发式 + 数据级验证——真实 key 重叠才发布 Join 提示。"""
        adapter = await sqlite_registry.get()
        await adapter.execute(
            "CREATE TABLE district (district_id INTEGER PRIMARY KEY, name TEXT)"
        )
        await adapter.execute(
            "CREATE TABLE city (city_id INTEGER PRIMARY KEY, district_id INTEGER)"
        )
        # 有重叠数据 → hint 发布
        await adapter.execute(
            "INSERT INTO district (district_id, name) VALUES (1, 'A'), (2, 'B')"
        )
        await adapter.execute("INSERT INTO city (city_id, district_id) VALUES (10, 1)")
        node = make_schema_linking(catalog=catalog, connectors=sqlite_registry)
        update = await node(make_state(question="city and district info"))
        assert "city.district_id → district.district_id" in update["schema_context"]

    async def test_join_hints_without_overlap_are_dropped(
        self, catalog, sqlite_registry,
    ):
        """命名对但数据对不上(空表/无交集)→ hint 不发布,防止错关联进 prompt。"""
        adapter = await sqlite_registry.get()
        await adapter.execute(
            "CREATE TABLE district (district_id INTEGER PRIMARY KEY, name TEXT)"
        )
        await adapter.execute(
            "CREATE TABLE city (city_id INTEGER PRIMARY KEY, district_id INTEGER)"
        )
        node = make_schema_linking(catalog=catalog, connectors=sqlite_registry)
        update = await node(make_state(question="city and district info"))
        assert "Join hints" not in update["schema_context"]


# ── Value linking ────────────────────────────────────────


class TestAlignment:
    """LLM 对齐裁剪(AskData Task Alignment):纯函数 + 节点集成。"""

    def _import(self, name):
        from trove.workflow.nodes import schema_linking
        return getattr(schema_linking, name)

    def test_parse_alignment_valid(self):
        _parse_alignment = self._import("_parse_alignment")
        assert _parse_alignment(
            '{"keep_tables": ["students"], "drop_columns": {"students": ["grade"]}}'
        ) == {"keep_tables": ["students"], "drop_columns": {"students": ["grade"]}}

    def test_parse_alignment_fenced_json(self):
        _parse_alignment = self._import("_parse_alignment")
        assert _parse_alignment('```json\n{"keep_tables": ["a"]}\n```') == {
            "keep_tables": ["a"], "drop_columns": {},
        }

    def test_parse_alignment_invalid_returns_none(self):
        _parse_alignment = self._import("_parse_alignment")
        assert _parse_alignment("just prose") is None
        assert _parse_alignment('{"keep_tables": "not-a-list"}') is None
        assert _parse_alignment("") is None

    def test_apply_alignment_filters_and_validates_columns(self):
        _apply_alignment = self._import("_apply_alignment")
        cols = {"students": {"grade", "name"}, "courses": {"id"}}
        keep, drop = _apply_alignment(
            ["students", "courses"],
            {"keep_tables": ["students"], "drop_columns": {
                "students": ["grade", "nope"]}},
            cols,
        )
        assert keep == ["students"]
        assert drop == {"students": {"grade"}}  # nope 校验到真实列后丢弃

    def test_apply_alignment_empty_keep_falls_back(self):
        _apply_alignment = self._import("_apply_alignment")
        keep, drop = _apply_alignment(
            ["a"], {"keep_tables": [], "drop_columns": {}}, {"a": set()},
        )
        assert keep == ["a"] and drop == {}

    def test_apply_alignment_must_keep_survives(self):
        """回退重跑时上一轮匹配表强制保留,对齐不允许丢。"""
        _apply_alignment = self._import("_apply_alignment")
        keep, _ = _apply_alignment(
            ["a", "b"], {"keep_tables": ["b"], "drop_columns": {}},
            {"a": set(), "b": set()}, must_keep=["a"],
        )
        assert keep == ["a", "b"]

    def test_alignment_context_includes_stats(self):
        _alignment_context = self._import("_alignment_context")
        from trove.services.kb.service import TableNotes
        details = [{"name": "students", "row_count": 5, "columns": [
            {"name": "grade", "type": "int"}]}]
        notes = {"students": TableNotes(
            row_count=5,
            stats={"grade": {"null_ratio": 0.9, "distinct": 5, "min": 1, "max": 10}},
        )}
        ctx = _alignment_context(details, notes)
        assert "Table: students (5 rows)" in ctx
        assert "90% NULL" in ctx and "1..10" in ctx

    def test_alignment_context_includes_top_values_and_join_hints(self):
        """P1-6:Top-K 值(列实际内容)与已验证 join hints 进对齐上下文。"""
        _alignment_context = self._import("_alignment_context")
        from trove.services.kb.service import TableNotes
        details = [
            {"name": "city", "row_count": 2, "columns": [
                {"name": "district_id", "type": "int"},
                {"name": "name", "type": "varchar"},
            ]},
            {"name": "district", "row_count": 2, "columns": [
                {"name": "district_id", "type": "int"},
            ]},
        ]
        notes = {"city": TableNotes(row_count=2, stats={"name": {
            "distinct": 2, "top_values": [["Alameda", 1], ["Orange", 1]],
        }})}
        hints = {"city": ["city.district_id → district.district_id (2/2 match)"]}
        ctx = _alignment_context(details, notes, hints)
        assert "Join hints: city.district_id → district.district_id (2/2 match)" in ctx
        assert "top values: Alameda (1), Orange (1)" in ctx

    async def test_alignment_trims_tables_and_columns(
        self, catalog, sqlite_registry, kb_service, kb_ds_dir,
    ):
        """对齐结果生效:表保序过滤,裁剪列不进 schema_context。"""
        (kb_ds_dir / "schema_notes.yml").write_text(
            """
tables:
  - name: students
    description: student records
    row_count: 5
    columns:
      - name: grade
        description: grade
        stats:
          null_ratio: 0.0
          distinct: 5
          min: 75
          max: 99
      - name: name
        description: name
        stats:
          null_ratio: 0.0
          distinct: 5
""",
            encoding="utf-8",
        )
        llm = ScriptedLLM(
            ['{"keep_tables": ["students"], "drop_columns": {"students": ["name"]}}']
        )
        node = make_schema_linking(
            catalog=catalog, kb=kb_service, connectors=sqlite_registry,
            llm=llm, config=AgentConfig(target="mock/model"),
        )
        update = await node(make_state(question="students"))
        assert update["matched_tables"] == ["students"]
        assert "grade (INTEGER)" in update["schema_context"]
        assert "name (TEXT)" not in update["schema_context"]
        assert "Stats:" in update["schema_context"]

    async def test_alignment_failure_falls_back(
        self, catalog, sqlite_registry, kb_service, kb_ds_dir,
    ):
        """LLM 输出不可解析 → 原样使用候选集,管线不阻塞。"""
        (kb_ds_dir / "schema_notes.yml").write_text(
            """
tables:
  - name: students
    description: student records
    columns:
      - name: grade
        description: grade
        stats:
          null_ratio: 0.9
""",
            encoding="utf-8",
        )
        llm = ScriptedLLM(["this is not json"])
        node = make_schema_linking(
            catalog=catalog, kb=kb_service, connectors=sqlite_registry,
            llm=llm, config=AgentConfig(target="mock/model"),
        )
        update = await node(make_state(question="students"))
        assert "students" in update["schema_context"]
        assert "grade (INTEGER)" in update["schema_context"]

    async def test_no_stats_skips_alignment(
        self, catalog, sqlite_registry, kb_service, kb_ds_dir,
    ):
        """无统计证据(KB 只有描述)→ 不发起对齐调用。"""
        (kb_ds_dir / "schema_notes.yml").write_text(
            """
tables:
  - name: students
    description: student records
    columns:
      - name: grade
        description: grade
""",
            encoding="utf-8",
        )

        class ExplodingLLM:
            async def chat(self, model, messages, **kwargs):
                raise AssertionError("alignment should not run without stats")

        node = make_schema_linking(
            catalog=catalog, kb=kb_service, connectors=sqlite_registry,
            llm=ExplodingLLM(), config=AgentConfig(target="mock/model"),
        )
        update = await node(make_state(question="students"))
        assert "students" in update["schema_context"]

    async def test_alignment_prompt_receives_hints_and_top_values(
        self, catalog, sqlite_registry, kb_service, kb_ds_dir,
    ):
        """P1-6:已验证 join hints 与 Top-K 值进入对齐上下文。

        对齐裁掉 district 后,hint(city→district)两端必须都在保留集,
        不再发布给 gen_sql——防止引用已裁表的错关联进 schema_context。
        """
        adapter = await sqlite_registry.get()
        await adapter.execute(
            "CREATE TABLE district (district_id INTEGER PRIMARY KEY, name TEXT)"
        )
        await adapter.execute(
            "CREATE TABLE city (city_id INTEGER PRIMARY KEY, district_id INTEGER, "
            "name TEXT)"
        )
        await adapter.execute(
            "INSERT INTO district (district_id, name) VALUES (1, 'A'), (2, 'B')"
        )
        await adapter.execute(
            "INSERT INTO city (city_id, district_id, name) VALUES "
            "(10, 1, 'Alameda'), (11, 2, 'Orange')"
        )
        (kb_ds_dir / "schema_notes.yml").write_text(
            """
tables:
  - name: city
    description: city records
    columns:
      - name: district_id
        description: district ref
        stats:
          null_ratio: 0.0
      - name: name
        description: city name
        stats:
          distinct: 2
          top_values:
            - ["Alameda", 1]
            - ["Orange", 1]
""",
            encoding="utf-8",
        )
        captured = {}

        class CapturingLLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(messages=messages)
                return '{"keep_tables": ["city"], "drop_columns": {}}'

        node = make_schema_linking(
            catalog=catalog, kb=kb_service, connectors=sqlite_registry,
            llm=CapturingLLM(), config=AgentConfig(target="mock/model"),
        )
        update = await node(make_state(question="city and district info"))
        prompt = " ".join(m["content"] for m in captured["messages"])
        assert "Join hints: city.district_id → district.district_id" in prompt
        assert "top values: Alameda (1), Orange (1)" in prompt
        # 对齐裁掉 district → hint 引用已裁表,不再发布
        assert "Join hints" not in update["schema_context"]


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


class TestVerifiedJoinHints:
    """P0-3:join hint 数据级验证——采样值重叠探测。"""

    @staticmethod
    def _scripted(values, hits, fail=False):
        import types

        class _C:
            def __init__(self):
                self.queries = []

            async def get(self):
                return types.SimpleNamespace(dialect=lambda: "sqlite")

            async def execute(self, sql):
                if fail:
                    raise RuntimeError("db down")
                self.queries.append(sql)
                if "SELECT COUNT(*)" in sql:
                    return types.SimpleNamespace(rows=[[hits]])
                return types.SimpleNamespace(rows=[[v] for v in values])

        return _C()

    HINT = "account.district_id → district.district_id"

    async def test_all_match_keeps_hint_plain(self):
        from trove.workflow.nodes.schema_linking import _verified_hints

        result = await _verified_hints(
            self._scripted([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 10), [self.HINT],
        )
        assert result == {self.HINT: self.HINT}

    async def test_partial_match_keeps_hint_with_ratio(self):
        from trove.workflow.nodes.schema_linking import _verified_hints

        result = await _verified_hints(
            self._scripted([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 7), [self.HINT],
        )
        assert result[self.HINT] == f"{self.HINT} (7/10 match)"

    async def test_below_min_match_drops(self):
        from trove.workflow.nodes.schema_linking import _verified_hints

        result = await _verified_hints(
            self._scripted([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 1), [self.HINT],
        )
        assert result == {}

    async def test_probe_failure_drops_silently(self):
        from trove.workflow.nodes.schema_linking import _verified_hints

        result = await _verified_hints(self._scripted([], 0, fail=True), [self.HINT])
        assert result == {}

    async def test_null_sample_drops(self):
        from trove.workflow.nodes.schema_linking import _verified_hints

        result = await _verified_hints(self._scripted([None, None], 0), [self.HINT])
        assert result == {}

    async def test_no_connectors_passthrough(self):
        from trove.workflow.nodes.schema_linking import _verified_hints

        assert await _verified_hints(None, [self.HINT]) == {self.HINT: self.HINT}

    async def test_mysql_uses_backtick_quoting(self):
        import types

        from trove.workflow.nodes.schema_linking import _verified_hints

        class _C:
            def __init__(self):
                self.queries = []

            async def get(self):
                return types.SimpleNamespace(dialect=lambda: "mysql")

            async def execute(self, sql):
                self.queries.append(sql)
                if "SELECT COUNT(*)" in sql:
                    return types.SimpleNamespace(rows=[[2]])
                return types.SimpleNamespace(rows=[[1], [2]])

        c = _C()
        await _verified_hints(c, [self.HINT])
        assert "`district_id`" in c.queries[0]


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

    def test_build_sql_prompt_from_state_matches_kwargs(self):
        """状态装配与直接 kwargs 产出一致:集中展开不改变 prompt 内容。"""
        from trove.workflow.state import GenSQLState

        state = GenSQLState(
            question="q",
            schema_context="schema",
            dialect="sqlite",
            lang="en",
            reflect_reason="wrong grouping",
            error_feedback="no such table: loans",
            evidence="hint",
            few_shots=[{"question": "x", "sql": "SELECT 1", "template": False}],
        )
        expected = build_sql_prompt(
            question="q",
            schema_context="schema",
            dialect="sqlite",
            reflect_reason="wrong grouping",
            error_feedback="no such table: loans",
            evidence="hint",
            few_shots=[{"question": "x", "sql": "SELECT 1", "template": False}],
        )
        assert build_sql_prompt_from_state(state) == expected

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

    def test_prompt_starts_with_stable_cache_prefix(self):
        """Prompt caching 布局:稳定前缀(dialect+schema)在开头,易变内容在尾部。"""
        prompt = build_sql_prompt(
            "q", "schema", "sqlite",
            few_shots=[{"question": "x", "sql": "SELECT 1"}],
            history="h",
        )
        assert prompt.startswith(render_cache_prefix("sqlite", "schema"))
        assert prompt.index("Database schema:") < prompt.index("Question:")
        assert prompt.index("Question:") < prompt.index("Generate the SQL query")

    def test_render_cache_prefix_matches_template_and_empty_schema(self):
        assert render_cache_prefix("sqlite", "") == (
            "Target SQL dialect: sqlite\n\nDatabase schema:\n"
            "(No schema information available - generate a best-effort query)\n"
        )
        # 与模板渲染输出的前缀逐字一致(估算/观测口径不漂移)
        rendered = render(
            "gen_sql/user", lang="en",
            question="q", schema_context="schema", dialect="sqlite",
        )
        assert rendered.startswith(render_cache_prefix("sqlite", "schema"))

    def test_build_sql_prompt_includes_error_feedback(self):
        prompt = build_sql_prompt("q", "schema", "sqlite", error_feedback="no such table: loans")
        assert "no such table: loans" in prompt
        assert "failed during execution" in prompt

    def test_build_sql_prompt_injects_rejected_hypotheses(self):
        """已试错的解释黑名单注入:模型必须知道哪些解释已被排除。"""
        prompt = build_sql_prompt("q", "schema", "sqlite", rejected_hypotheses=[
            {"sql": "SELECT * FROM loans", "reason": "table does not exist"},
        ])
        assert "Rejected hypotheses" in prompt
        assert "SELECT * FROM loans" in prompt
        assert "table does not exist" in prompt

    def test_build_sql_prompt_without_hypotheses_has_no_section(self):
        assert "Rejected hypotheses" not in build_sql_prompt("q", "schema", "sqlite")

    def test_build_sql_prompt_repair_verification_mandatory_on_fix_rounds(self):
        """修复轮必须强制自证:error_feedback/error_analysis 时注入 verification 指令,
        正常生成轮不出现(避免污染无错误的首轮生成)。"""
        fix = build_sql_prompt("q", "schema", "sqlite", error_feedback="no such table: loans")
        assert "Repair verification (mandatory)" in fix
        assert "probe_query" in fix and "check_result" in fix
        assert "Never submit an unverified repair" in fix

        rework = build_sql_prompt("q", "schema", "sqlite", error_analysis="misread intent")
        assert "Repair verification (mandatory)" in rework

        first = build_sql_prompt("q", "schema", "sqlite")
        assert "Repair verification" not in first
        # 仅 reflect 打回(无执行失败)也不算修复轮
        reflected = build_sql_prompt("q", "schema", "sqlite", reflect_reason="wrong grouping")
        assert "Repair verification" not in reflected

    def test_build_sql_prompt_injects_previous_sql_for_local_fix(self):
        """Fixer 模式:打回轮注入上一版 SQL 全文,指示局部修复而非整体重写。"""
        prompt = build_sql_prompt(
            "q", "schema", "sqlite",
            error_feedback="rule failed",
            previous_sql="SELECT name FROM students WHERE 0;",
        )
        assert "Previous SQL" in prompt
        assert "SELECT name FROM students WHERE 0;" in prompt
        assert "minimal" in prompt  # 局部修复指令

    def test_build_sql_prompt_without_previous_sql_has_no_section(self):
        assert "Previous SQL" not in build_sql_prompt("q", "schema", "sqlite")

    def test_build_sql_prompt_injects_sql_versions(self):
        """定点修复:失败版本链(SQL + 签名 + 规则命中)注入生成 prompt。"""
        prompt = build_sql_prompt("q", "schema", "sqlite", sql_versions=[
            {"sql": "SELECT * FROM loans", "sig": "sig1", "issues": ["F1-b"], "round": 1},
            {"sql": "SELECT * FROM loan", "sig": "sig2", "issues": ["F1-b"], "round": 2},
        ])
        assert "Failed SQL versions" in prompt
        assert "Round 1" in prompt
        assert "Round 2" in prompt
        assert "SELECT * FROM loans" in prompt
        assert "F1-b" in prompt

    def test_build_sql_prompt_without_versions_has_no_section(self):
        assert "Failed SQL versions" not in build_sql_prompt("q", "schema", "sqlite")

    def test_build_sql_prompt_renders_fixer_mode(self):
        """Fixer 模式:显式指令「保持语义解释不变,只修实现」。"""
        prompt = build_sql_prompt("q", "schema", "sqlite", fix_mode="fixer")
        assert "implementation-level repair" in prompt
        assert "keep the semantic interpretation unchanged" in prompt

    def test_build_sql_prompt_renders_revisor_mode(self):
        """Revisor 模式:显式指令「语义解释本身可能错,重新评估意图」。"""
        prompt = build_sql_prompt("q", "schema", "sqlite", fix_mode="revisor")
        assert "semantic rework" in prompt
        assert "re-evaluate the question's intent" in prompt

    def test_build_sql_prompt_without_fix_mode_has_no_section(self):
        assert "Fix mode" not in build_sql_prompt("q", "schema", "sqlite")

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


# ── probe_query(只读执行探针)──────────────────────────


class TestProbeQuery:
    async def test_probe_returns_observation(self, sqlite_registry):
        """正常只读探针:ok + 真实行数(COUNT 包装)+ 列 + 前 5 行。"""
        import json
        from trove.workflow.nodes.gen_sql import probe_query

        obs = json.loads(await probe_query(sqlite_registry, "SELECT name FROM students", "sqlite"))
        assert obs["ok"] is True
        assert obs["row_count"] == 5
        assert obs["columns"] == ["name"]
        assert len(obs["rows"]) == 5

    async def test_probe_respects_existing_limit(self, sqlite_registry):
        """已有 LIMIT 时不重写、不做 COUNT 包装——行数就是 LIMIT 值。"""
        import json
        from trove.workflow.nodes.gen_sql import probe_query

        obs = json.loads(await probe_query(sqlite_registry, "SELECT name FROM students LIMIT 2", "sqlite"))
        assert obs["ok"] is True
        assert obs["row_count"] == 2
        assert len(obs["rows"]) == 2

    async def test_probe_rejects_write_operations(self, sqlite_registry):
        """只读门:DROP/DELETE/INSERT/UPDATE 一律 ok:false,且不执行。"""
        import json
        from trove.workflow.nodes.gen_sql import probe_query

        for sql in ("DELETE FROM students", "INSERT INTO students (name) VALUES ('X')",
                    "DROP TABLE students", "UPDATE students SET grade = 0"):
            obs = json.loads(await probe_query(sqlite_registry, sql, "sqlite"))
            assert obs["ok"] is False, sql
            assert "write" in obs["error"], sql
        # 表仍在(只读性验证)
        r = await sqlite_registry.execute("SELECT COUNT(*) FROM students")
        assert r.rows[0][0] == 5

    async def test_probe_rejects_multi_statement(self, sqlite_registry):
        """多语句被拦截:AST 防火墙(Block)或 sqlglot 校验层均可。"""
        import json
        from trove.workflow.nodes.gen_sql import probe_query

        obs = json.loads(await probe_query(
            sqlite_registry, "SELECT 1; SELECT 2", "sqlite"))
        assert obs["ok"] is False
        assert "Multiple" in obs["error"] or "only SELECT" in obs["error"]

    async def test_probe_syntax_error(self, sqlite_registry):
        import json
        from trove.workflow.nodes.gen_sql import probe_query

        obs = json.loads(await probe_query(sqlite_registry, "SELEC * FROM students", "sqlite"))
        assert obs["ok"] is False

    async def test_probe_rejects_metadata_table(self, sqlite_registry):
        """元数据侦察(sqlite_master)在注册表执行层被统一拦截。"""
        import json
        from trove.workflow.nodes.gen_sql import probe_query

        obs = json.loads(await probe_query(
            sqlite_registry, "SELECT * FROM sqlite_master", "sqlite"))
        assert obs["ok"] is False
        assert "metadata" in obs["error"]

    async def test_probe_rejects_data_modifying_cte(self, sqlite_registry):
        """data-modifying CTE:顶层是 SELECT,树内藏 DELETE — AST 整树扫描拦截。"""
        import json
        from trove.workflow.nodes.gen_sql import probe_query

        obs = json.loads(await probe_query(
            sqlite_registry,
            "WITH x AS (DELETE FROM students RETURNING *) SELECT * FROM x",
            "sqlite"))
        assert obs["ok"] is False
        assert "write operation" in obs["error"]

    async def test_probe_allowlist(self, sqlite_registry):
        """allowed_tables 约束:表在集合内放行,集合外拒绝。"""
        import json
        from trove.workflow.nodes.gen_sql import probe_query

        ok = json.loads(await probe_query(
            sqlite_registry, "SELECT name FROM students", "sqlite",
            allowed_tables={"students"}))
        assert ok["ok"] is True
        denied = json.loads(await probe_query(
            sqlite_registry, "SELECT name FROM students", "sqlite",
            allowed_tables={"other"}))
        assert denied["ok"] is False
        assert "not in the allowed tables" in denied["error"]

    def test_has_limit_unparseable_defaults_to_no_limit(self):
        """LIMIT 判定保守方向:无法确认 → 按无 LIMIT 处理(注入封顶),
        而不是按「已有 LIMIT」放行(那会让全表查询无上限执行)。"""
        from trove.workflow.nodes.gen_sql import _has_limit

        assert _has_limit("SELECT name FROM students LIMIT 2", "sqlite") is True
        assert _has_limit("SELECT name FROM students", "sqlite") is False
        assert _has_limit("SELEC * FROM students", "sqlite") is False  # 无法解析

    async def test_probe_timeout_folded_into_observation(self):
        """超时折叠成 ok:false 观测,不抛异常。"""
        import asyncio
        import json
        from trove.workflow.nodes.gen_sql import probe_query

        class SlowConnector:
            async def execute(self, sql, datasource=None):
                await asyncio.sleep(5)
                raise AssertionError("should not finish")

        obs = json.loads(await probe_query(SlowConnector(), "SELECT 1", "sqlite", timeout_s=0.01))
        assert obs["ok"] is False
        assert "timed out" in obs["error"]

    async def test_probe_no_connectors(self):
        import json
        from trove.workflow.nodes.gen_sql import probe_query

        obs = json.loads(await probe_query(None, "SELECT 1", "sqlite"))
        assert obs["ok"] is False
        assert "no datasource" in obs["error"]

    async def test_probe_accepts_cte(self, sqlite_registry):
        """CTE 查询通过只读门,行数正确(COUNT 包装包裹整个 WITH 查询)。"""
        import json
        from trove.workflow.nodes.gen_sql import probe_query

        obs = json.loads(await probe_query(
            sqlite_registry,
            "WITH x AS (SELECT id, name FROM students) SELECT name FROM x",
            "sqlite"))
        assert obs["ok"] is True
        assert obs["row_count"] == 5


# ── _column_stats_text(planner 列画像)─────────────────


class TestColumnStats:
    async def test_county_stats(self, sqlite_registry):
        """rows/null 比例/distinct/样例/低基数 top 一应俱全。"""
        from trove.workflow.nodes.planner import _column_stats_text

        text = await _column_stats_text(sqlite_registry, "students", "county")
        assert "rows=5" in text
        assert "null_ratio=0.0" in text
        assert "distinct=3" in text
        assert "Alameda" in text  # 样例
        assert "Alameda (2)" in text  # 低频 top:频次
        assert "Los Angeles (1)" in text

    async def test_unknown_table(self, sqlite_registry):
        from trove.workflow.nodes.planner import _column_stats_text

        text = await _column_stats_text(sqlite_registry, "nope", "county")
        assert "not found" in text
        assert "nope" in text

    async def test_unknown_column(self, sqlite_registry):
        from trove.workflow.nodes.planner import _column_stats_text

        text = await _column_stats_text(sqlite_registry, "students", "nope")
        assert "not found" in text
        assert "nope" in text

    async def test_no_connectors(self):
        from trove.workflow.nodes.planner import _column_stats_text

        text = await _column_stats_text(None, "students", "county")
        assert "no datasource" in text


# ── extra_columns_mismatch(层2多余列检查)───────────────


class TestExtraColumnsMismatch:
    def test_extra_sort_column_caught(self):
        """结果列含 plan 答案列之外、问题也未点名的列 → 冲突。"""
        from trove.workflow.nodes.planner import extra_columns_mismatch

        errors = extra_columns_mismatch(
            {"answer_columns": ["account_id"]},
            ["account_id", "amount"],
            "list the accounts",
        )
        assert len(errors) == 1
        assert "amount" in errors[0]
        assert "account_id" in errors[0]

    def test_question_named_column_exempt(self):
        """问题点名了某列而 plan 漏写 → 规则 19 允许的偏离,豁免。"""
        from trove.workflow.nodes.planner import extra_columns_mismatch

        # 复数 + 下划线变体
        assert extra_columns_mismatch(
            {"answer_columns": ["unemployment_rate"]},
            ["unemployment_rate", "district"],
            "list the districts and their unemployment rate",
        ) == []
        # 下划线 vs 空格
        assert extra_columns_mismatch(
            {"answer_columns": ["avg_balance"]},
            ["avg_balance", "account_type"],
            "what is the account type and average balance",
        ) == []

    def test_alias_and_qualified_refs_pass(self):
        """answer ref 带表限定、结果列是别名 → 尾缀匹配,无多余列。"""
        from trove.workflow.nodes.planner import extra_columns_mismatch

        assert extra_columns_mismatch(
            {"answer_columns": ["loan.account_id"]},
            ["account_id"],
            "list the accounts",
        ) == []

    def test_missing_answer_column_deferred(self):
        """答案列有缺失 → 交给层2主检查(宁漏勿误,不双重打回)。"""
        from trove.workflow.nodes.planner import extra_columns_mismatch

        assert extra_columns_mismatch(
            {"answer_columns": ["account_id", "client_id"]},
            ["account_id"],
            "list the accounts",
        ) == []

    def test_no_plan_or_no_refs(self):
        from trove.workflow.nodes.planner import extra_columns_mismatch

        assert extra_columns_mismatch(None, ["x"], "q") == []
        assert extra_columns_mismatch({}, ["x"], "q") == []
        assert extra_columns_mismatch({"answer_columns": ["*"]}, ["x"], "q") == []


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

    async def test_reflect_empty_verdict_reask_then_deliver(self):
        """主裁决不可解析 → 极简 prompt 再问一次;仍不可解析 → 强制放行
        (不能把正确结果推入升温重生成的搅动)。"""
        from trove.workflow.nodes.reflect import make_reflect

        class EmptyLLM:
            async def chat(self, model, messages, **kwargs):
                return ""

        node = make_reflect(EmptyLLM(), AgentConfig(target="m"))
        update = await node(make_state(
            question="what is the increase rate", row_count=1,
            columns=["a", "b", "c"], rows=[[1, 2, 3]],
        ))
        assert update["verdict"] == "OK"
        assert update["forced"] is True
        assert "unparseable" in update["reason"]

    async def test_reflect_reask_recovers_verdict(self):
        """再问一次拿到真实裁决(RETRY) → 走正常语义修正路径。"""
        from trove.workflow.nodes.reflect import make_reflect

        class LLM:
            def __init__(self):
                self.calls = 0

            async def chat(self, model, messages, **kwargs):
                self.calls += 1
                return "" if self.calls == 1 else "RETRY: columns look wrong"

        llm = LLM()
        node = make_reflect(llm, AgentConfig(target="m"))
        update = await node(make_state(
            question="what is the increase rate", row_count=1,
            columns=["a", "b", "c"], rows=[[1, 2, 3]],
        ))
        assert update["verdict"] == "RETRY"
        assert "columns look wrong" in update["reason"]
        assert update["retry_count"] == 1
        assert llm.calls == 2


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


class TestPlannerAgentic:
    """planner agentic 路径(带 connectors 的 ReAct 循环)——首个覆盖测试。"""

    async def test_planner_get_column_stats_round(self, sqlite_registry):
        """模型先调 get_column_stats 拿真实画像,再交 plan JSON;观测进对话。"""
        import json
        from trove.workflow.nodes.planner import make_planner

        class AgenticLLM:
            def __init__(self, responses):
                self._responses = list(responses)
                self.calls = []

            async def chat_full(self, model, messages, tools=None, **kwargs):
                self.calls.append(messages)
                return self._responses.pop(0)

            async def chat(self, model, messages, **kwargs):
                self.calls.append(messages)
                return self._responses.pop(0)

        plan = json.dumps({
            "tables": ["students"],
            "joins": "",
            "conditions": [],
            "aggregation": "none",
            "answer_columns": ["county"],
        })
        llm = AgenticLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "get_column_stats",
                 "arguments": '{"table": "students", "column": "county"}'},
            ]},
            {"content": plan, "tool_calls": []},
        ])
        node = make_planner(llm, AgentConfig(target="m"), connectors=sqlite_registry)
        update = await node(make_state(question="how are students distributed by county"))
        # 观测进 tool 消息:真实画像(distinct/null 比例/样例)
        tool_msgs = [m for msgs in llm.calls for m in msgs if m.get("role") == "tool"]
        assert any("distinct=3" in m["content"] and "Alameda" in m["content"] for m in tool_msgs)
        # plan 落地并通过层1校验
        assert update["plan"]
        assert update["plan_json"]["answer_columns"] == ["county"]
        assert update["plan_validation"]["status"] == "ok"


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
        # 全过 → 正向信号 rules_passed,供 reflect 决定是否跳过 LLM 裁决
        assert update == {"rules_passed": True}

    async def test_extra_columns_rule_gives_feedback(self):
        """结果列超出 plan 的 answer_columns → extra-columns 命中打回。"""
        from trove.workflow.nodes.validate import make_validate_rules

        node = make_validate_rules()
        state = make_state(
            question="list all the students",
            sql="SELECT name, grade FROM students",
            columns=["name", "grade"],
            rows=[["a", 1], ["b", 2]],
            row_count=2,
            plan_json={"answer_columns": ["name"]},
        )
        update = await node(state)
        assert "error" not in update
        assert "answer_columns" in update["error_feedback"]
        assert update["retry_count"] == 1
        assert update["validation_hits"][0]["rule"] == "extra-columns"

    async def test_extra_columns_question_named_column_passes(self):
        """结果列被问题点名(plan 漏写)→ 豁免,不误伤。"""
        from trove.workflow.nodes.validate import make_validate_rules

        node = make_validate_rules()
        state = make_state(
            question="list the districts and their unemployment rate",
            sql="SELECT district, unemployment_rate FROM districts",
            columns=["district", "unemployment_rate"],
            rows=[["a", 0.1]],
            row_count=1,
            plan_json={"answer_columns": ["unemployment_rate"]},
        )
        assert await node(state) == {"rules_passed": True}

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
        assert "SELECT county" in " ".join(response.split())  # pretty-printed SQL
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
    """① planner 作用域原则 + ③ reflect 条件完整性检查(冷启动语义防线)。

    常量已迁入 trove/prompts/*.j2 模板,这里直接渲染模板断言子串。
    """

    def test_planner_prompt_carries_scope_principle(self):
        en = render("planner/system", lang="en")
        zh = render("planner/system", lang="zh")
        assert "lowest" in en.lower()
        assert "scope" in en.lower()
        assert "作用域" in zh
        assert "最低" in zh

    def test_gen_prompt_carries_generalized_lessons(self):
        """Hint Bank 通用教训升入 system prompt(跨数据集生效):
        极值 ORDER BY LIMIT 1 / 多重最高级单排序 / 直接 FK join / 按点名实体选择。"""
        en = render("gen_sql/system", lang="en")
        zh = render("gen_sql/system", lang="zh")
        assert "CTE" in en  # 极值:不用 CTE/嵌套子查询
        assert "breaks ties" in en  # 多重最高级:次级条件只裁决平局
        assert "row granularity" in en  # 直接 FK join
        assert "entity column" in en  # 按点名实体分组/选择
        assert "CTE" in zh
        assert "平局" in zh
        assert "行粒度" in zh
        assert "实体列" in zh

    def test_gen_prompt_carries_critical_rules_and_probe_tools(self):
        """前置 CRITICAL RULES 块 + has_probe 门控工具段落(默认不出现)。"""
        en = render("gen_sql/system", lang="en")
        zh = render("gen_sql/system", lang="zh")
        assert "CRITICAL RULES" in en
        assert "关键规则" in zh
        # 既有锚字符串保持不动(规则 16/19 等未改编号)
        assert "breaks ties" in en
        assert "平局" in zh
        assert "row granularity" in en
        # has_probe=True → 工具段落出现;默认(无 has_probe)→ 不出现
        en_probe = render("gen_sql/system", lang="en", has_probe=True)
        zh_probe = render("gen_sql/system", lang="zh", has_probe=True)
        assert "probe_query" in en_probe
        assert "probe_query" in zh_probe
        assert "probe_query" not in en
        assert "probe_query" not in zh

    def test_planner_prompt_carries_tools_gated_by_has_tools(self):
        """planner 工具段落:has_tools=True 含 get_column_stats,False 不含。"""
        en = render("planner/system", lang="en", has_tools=True)
        zh = render("planner/system", lang="zh", has_tools=True)
        assert "get_column_stats" in en
        assert "get_column_stats" in zh
        assert "get_column_stats" not in render("planner/system", lang="en")
        assert "get_column_stats" not in render("planner/system", lang="zh")

    def test_reflect_prompt_carries_condition_completeness(self):
        en = render("reflect/system", lang="en")
        zh = render("reflect/system", lang="zh")
        assert "every condition" in en
        assert "每个条件" in zh

    def test_reflect_prompt_guards_against_rearguing_ambiguity(self):
        """法官不得重新争论问题歧义,也不得用'并列可能漏行'打回 LIMIT 1。"""
        en = render("reflect/system", lang="en")
        zh = render("reflect/system", lang="zh")
        assert "interpretation" in en
        assert "LIMIT 1" in en
        assert "合理解读" in zh
        assert "并列" in zh
        assert "formula" in en
        assert "公式" in zh


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


class TestPlanValidation:
    """P0-2 层1:计划落地校验(表/列存在性)与自修正/丢弃。"""

    SCHEMA = {
        "loan": {"account_id", "amount", "date", "status"},
        "account": {"account_id", "district_id", "frequency"},
    }

    def test_valid_plan_passes(self):
        from trove.workflow.nodes.planner import validate_plan
        plan = {
            "tables": ["loan", "account"],
            "answer_columns": ["account_id", "amount"],
            "conditions": [{"field": "loan.status", "op": "=", "value": "OK"}],
        }
        assert validate_plan(plan, self.SCHEMA) == []

    def test_unknown_table_fails(self):
        from trove.workflow.nodes.planner import validate_plan
        plan = {"tables": ["loan", "nonexistent"], "answer_columns": ["account_id"]}
        errors = validate_plan(plan, self.SCHEMA)
        assert any("nonexistent" in e for e in errors)

    def test_unknown_column_fails(self):
        from trove.workflow.nodes.planner import validate_plan
        plan = {"tables": ["loan"], "answer_columns": ["account_id", "ghost_col"]}
        errors = validate_plan(plan, self.SCHEMA)
        assert any("ghost_col" in e for e in errors)

    def test_table_dot_column_form_checked(self):
        from trove.workflow.nodes.planner import validate_plan
        plan = {"tables": ["loan"], "conditions": [{"field": "loan.missing"}]}
        errors = validate_plan(plan, self.SCHEMA)
        assert any("missing" in e for e in errors)
        plan_ok = {"tables": ["loan"], "conditions": [{"field": "loan.amount"}]}
        assert validate_plan(plan_ok, self.SCHEMA) == []

    def test_expressions_and_wildcards_skipped(self):
        from trove.workflow.nodes.planner import validate_plan
        plan = {
            "tables": ["loan"],
            "answer_columns": ["COUNT(*)", "AVG(amount)", "*", ""],
        }
        assert validate_plan(plan, self.SCHEMA) == []

    def test_case_insensitive(self):
        from trove.workflow.nodes.planner import validate_plan
        plan = {"tables": ["LOAN"], "answer_columns": ["Account_ID"]}
        assert validate_plan(plan, self.SCHEMA) == []

    def test_no_schema_or_no_plan_skips(self):
        from trove.workflow.nodes.planner import validate_plan
        assert validate_plan(None, self.SCHEMA) == []
        assert validate_plan({"tables": ["x"]}, None) == []

    def test_answer_columns_mismatch_all_missing_is_conflict(self):
        from trove.workflow.nodes.planner import answer_columns_mismatch
        plan = {"answer_columns": ["account_id", "frequency"]}
        errs = answer_columns_mismatch(plan, ["status", "amount"])
        assert errs and "conflict" in errs[0]

    def test_answer_columns_mismatch_partial_match_passes(self):
        """别名/表达式噪音:至少一个命中即放行。"""
        from trove.workflow.nodes.planner import answer_columns_mismatch
        plan = {"answer_columns": ["account_id", "total"]}
        assert answer_columns_mismatch(plan, ["account_id", "SUM(amount) AS total"]) == []

    def test_answer_columns_mismatch_skipped_without_plan(self):
        from trove.workflow.nodes.planner import answer_columns_mismatch
        assert answer_columns_mismatch(None, ["a"]) == []
        assert answer_columns_mismatch({"answer_columns": []}, ["a"]) == []
        assert answer_columns_mismatch({"answer_columns": ["COUNT(*)"]}, ["a"]) == []

    @staticmethod
    def _connectors():
        """带 loan/account 两表的 connectors mock(触发落地校验)。"""
        import types

        class _C:
            async def get_schema(self):
                table = lambda name, cols: types.SimpleNamespace(
                    name=name,
                    columns=[types.SimpleNamespace(name=c) for c in cols],
                )
                return types.SimpleNamespace(tables=[
                    table("loan", ["account_id", "amount", "date", "status"]),
                    table("account", ["account_id", "district_id", "frequency"]),
                ])

        return _C()

    async def test_node_drops_invalid_plan(self):
        """校验不过且修正后仍不过 → 丢弃 plan(gen_sql 不受幻觉列挟持)。"""
        from trove.workflow.nodes.planner import make_planner

        class LLM:
            def __init__(self):
                self.calls = 0

            async def chat(self, model, messages, **kwargs):
                self.calls += 1
                return (
                    '{"tables": ["loan", "ghost"], '
                    '"answer_columns": ["ghost_col", "COUNT(*)"]}'
                )

        llm = LLM()
        node = make_planner(
            llm, AgentConfig(target="m"), agentic=False,
            connectors=self._connectors(),
        )
        update = await node(make_state())
        assert update["plan"] == ""
        assert update["plan_json"] is None
        assert update["plan_validation"]["status"] == "dropped"
        assert any("ghost" in e for e in update["plan_validation"]["errors"])
        assert llm.calls == 2  # 自修正一次后仍无效才丢弃

    async def test_node_self_retries_then_accepts_fixed_plan(self):
        """修正轮产出合法计划 → 采纳并携带 plan_json。"""
        from trove.workflow.nodes.planner import make_planner

        responses = [
            '{"tables": ["loan", "ghost"], "answer_columns": ["amount"]}',
            '{"tables": ["loan"], "answer_columns": ["amount"]}',
        ]

        class LLM:
            def __init__(self):
                self.calls = 0
                self.prompts = []

            async def chat(self, model, messages, **kwargs):
                self.prompts.append(" ".join(m["content"] for m in messages))
                r = responses[self.calls]
                self.calls += 1
                return r

        llm = LLM()
        node = make_planner(
            llm, AgentConfig(target="m"), agentic=False,
            connectors=self._connectors(),
        )
        update = await node(make_state())
        assert llm.calls == 2
        assert "invalid" in llm.prompts[1]  # 修正提示携带具体错误
        assert update["plan_json"] == {"tables": ["loan"], "answer_columns": ["amount"]}
        assert update["plan_validation"]["status"] == "ok"

    async def test_planner_prompt_includes_evidence(self):
        """P0-1:planner 起草 answer_columns 时能看到官方 evidence。"""
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured["user"] = messages[1]["content"]
                return "plan text"

        from trove.workflow.nodes.planner import make_planner

        node = make_planner(LLM(), AgentConfig(target="m"), agentic=False)
        await node(make_state(evidence="Answer should list district names", lang="en"))
        assert "Answer should list district names" in captured["user"]
        assert "official hint" in captured["user"]


class TestValidateRulesNode:
    """确定性规则节点:失败时输出 validation_hits 供归因。"""

    async def test_rule_failure_reports_hits(self):
        from trove.workflow.nodes.validate import make_validate_rules

        node = make_validate_rules(max_retries=3)
        state = make_state(
            question=(
                "List all the withdrawals in cash transactions that the "
                "client with the id 3356 makes."
            ),
            sql=("SELECT trans_id, account_id, date, type, operation, "
                 "amount, balance, k_symbol, bank FROM trans"),
            columns=["trans_id", "account_id", "date", "type", "operation",
                     "amount", "balance", "k_symbol", "bank"],
            rows=[["1"] * 9],
            row_count=1,
            lang="en",
        )
        update = await node(state)
        assert update["error_feedback"] and "[F1-b]" in update["error_feedback"]
        assert update["validation_hits"] and update["validation_hits"][0]["name"] == "F1-b"
        assert update["retry_count"] == 1

    async def test_rule_pass_returns_empty(self):
        from trove.workflow.nodes.validate import make_validate_rules

        node = make_validate_rules(max_retries=3)
        state = make_state(
            question="average loan amount",
            sql="SELECT AVG(amount) FROM loan",
            columns=["avg"], rows=[[123.4]], row_count=1, lang="en",
        )
        update = await node(state)
        assert update == {"rules_passed": True}

    async def test_answer_columns_conflict_feeds_back(self):
        """P0-2 层2:结果列整体背离 plan 的 answer_columns → 打回(归因 planner)。"""
        from trove.workflow.nodes.validate import make_validate_rules

        node = make_validate_rules(max_retries=3)
        state = make_state(
            question="list account frequency",
            sql="SELECT status, amount FROM loan",
            columns=["status", "amount"],
            rows=[["active", 1.0]], row_count=1, lang="en",
            plan_json={"tables": ["loan"], "answer_columns": ["account_id", "frequency"]},
        )
        update = await node(state)
        assert update["error_feedback"] and "answer_columns" in update["error_feedback"]
        assert update["validation_hits"][0]["rule"] == "answer-columns"
        assert update["retry_count"] == 1

    async def test_answer_columns_partial_match_passes(self):
        from trove.workflow.nodes.validate import make_validate_rules

        node = make_validate_rules(max_retries=3)
        state = make_state(
            question="list account frequency",
            sql="SELECT account_id, frequency FROM loan",
            columns=["account_id", "frequency"],
            rows=[[1, "x"]], row_count=1, lang="en",
            plan_json={"tables": ["loan"], "answer_columns": ["account_id", "frequency"]},
        )
        assert await node(state) == {"rules_passed": True}

    async def test_answer_columns_no_plan_skips(self):
        from trove.workflow.nodes.validate import make_validate_rules

        node = make_validate_rules(max_retries=3)
        state = make_state(
            question="average loan amount",
            sql="SELECT AVG(amount) FROM loan",
            columns=["avg"], rows=[[123.4]], row_count=1, lang="en",
            plan_json=None,
        )
        assert await node(state) == {"rules_passed": True}


class TestMakeSQLTools:
    """gen_sql ReAct 循环的工具工厂:工具集合、handler 绑定、hits_sink 归因。"""

    async def test_without_connectors_only_syntax_tool(self):
        """connectors 缺失 → 仅 validate_sql(纯语法),无执行类工具,hits_sink 为空。"""
        tools, handlers, hits = make_sql_tools(None, "q", "en", "sqlite")
        names = [t["function"]["name"] for t in tools]
        assert names == ["validate_sql"]
        assert list(handlers) == ["validate_sql"]
        assert hits == []
        assert await handlers["validate_sql"]({"sql": "SELECT 1"}) == "valid"
        assert "ERRORS" in await handlers["validate_sql"]({"sql": "SELEC 1"})

    async def test_with_connectors_five_tools_and_hits_sink(self, sqlite_registry):
        """connectors 就位 → 六工具(含 lookup_schema 懒加载 + explain_plan);check_tool 命中写 hits_sink,probe_tool 返回观测。"""
        tools, handlers, hits = make_sql_tools(
            sqlite_registry, "How many students are there in total?", "en", "sqlite",
        )
        names = [t["function"]["name"] for t in tools]
        assert names == ["validate_sql", "probe_query", "check_result", "search_values", "lookup_schema", "explain_plan"]
        # lookup_schema:懒加载表 DDL
        assert '"columns"' in await handlers["lookup_schema"]({"table": "students"})
        assert '"ok": false' in await handlers["lookup_schema"]({"table": "nope"})
        # check_tool:count 题分组展开草稿 → VIOLATION,命中进 hits_sink
        text = await handlers["check_result"]({
            "sql": "SELECT county, COUNT(*) FROM students GROUP BY county",
        })
        assert text.startswith("VIOLATION")
        assert [h["name"] for h in hits] == ["count-multirow"]
        # probe_tool:观测 JSON,真实行数
        obs = await handlers["probe_query"]({"sql": "SELECT name FROM students"})
        assert '"ok": true' in obs and '"row_count"' in obs
        # 无规则命中时 hits_sink 不被污染(与 count 题一致的合规 SQL)
        assert await handlers["check_result"]({"sql": "SELECT COUNT(*) FROM students"}) == "OK (1 rows)"
        assert [h["name"] for h in hits] == ["count-multirow"]
        # search_tool:值检索 JSON,大小写不敏感命中
        found = await handlers["search_values"]({
            "table": "students", "keyword": "ala",
        })
        assert '"hits"' in found and "Alameda" in found
        # explain_tool:执行计划 JSON,非空行;错误折叠
        plan = await handlers["explain_plan"]({"sql": "SELECT name FROM students"})
        data = json.loads(plan)
        assert data["ok"] is True and data["plan"] and "scan" in data["plan"][0].lower()
        assert json.loads(await handlers["explain_plan"]({"sql": "SELEC broken"}))["ok"] is False
        assert json.loads(await handlers["explain_plan"]({}))["ok"] is False


class TestStaticSemanticWarnings:
    """静态语义检查:纯 AST 启发式,只警告不拦截。"""

    @staticmethod
    def _schema():
        from trove.core.types import ColumnInfo, SchemaInfo, TableInfo

        return SchemaInfo(tables=[
            TableInfo(name="students", columns=[
                ColumnInfo(name="id", type="INTEGER"),
                ColumnInfo(name="name", type="TEXT"),
                ColumnInfo(name="grade", type="REAL"),
                ColumnInfo(name="county", type="TEXT"),
            ]),
            TableInfo(name="courses", columns=[
                ColumnInfo(name="id", type="INTEGER"),
                ColumnInfo(name="title", type="TEXT"),
            ]),
        ])

    # ── C1:问题提及的表必须出现在 SQL ──
    def test_c1_mentioned_table_missing_warns(self):
        out = static_semantic_warnings(
            "SELECT COUNT(*) FROM pupils", "sqlite",
            "How many students are there?", ["students"], self._schema(),
        )
        assert any("'students'" in w and "not reference" in w for w in out)

    def test_c1_mentioned_table_present_ok(self):
        out = static_semantic_warnings(
            "SELECT COUNT(*) FROM students", "sqlite",
            "How many students are there?", ["students"], self._schema(),
        )
        assert out == []

    def test_c1_unmentioned_matched_table_silent(self):
        """matched 但问题未字面提到 → 不触发(弱信号)。"""
        out = static_semantic_warnings(
            "SELECT COUNT(*) FROM students", "sqlite",
            "How many students are there?", ["students", "courses"], self._schema(),
        )
        assert out == []

    # ── C2:JOIN ON 列必须存在 ──
    def test_c2_missing_join_column_warns(self):
        out = static_semantic_warnings(
            "SELECT * FROM students s JOIN courses c ON s.id = c.nope", "sqlite",
            "students with courses", ["students", "courses"], self._schema(),
        )
        assert any("'nope'" in w and "'courses'" in w for w in out)

    def test_c2_valid_join_ok_and_alias_resolution(self):
        out = static_semantic_warnings(
            "SELECT * FROM students s JOIN courses c ON s.id = c.id", "sqlite",
            "students with courses", ["students", "courses"], self._schema(),
        )
        assert out == []

    # ── C3:列类型 × 字面量类型错配 ──
    def test_c3_numeric_column_vs_string_literal(self):
        out = static_semantic_warnings(
            "SELECT * FROM students WHERE grade = 'high'", "sqlite",
            "students by grade", ["students"], self._schema(),
        )
        assert any("numeric column 'grade'" in w and "string literal" in w for w in out)

    def test_c3_string_column_vs_number(self):
        out = static_semantic_warnings(
            "SELECT * FROM students WHERE name = 42", "sqlite",
            "students named 42", ["students"], self._schema(),
        )
        assert any("string column 'name'" in w and "numeric literal" in w for w in out)

    def test_c3_matching_types_ok_and_like_skipped(self):
        out = static_semantic_warnings(
            "SELECT * FROM students WHERE grade > 90 AND name LIKE 'A%'", "sqlite",
            "top students", ["students"], self._schema(),
        )
        assert out == []

    # ── schema 缺失退化 ──
    def test_no_schema_degrades_to_c1_only(self):
        out = static_semantic_warnings(
            "SELECT * FROM students s JOIN courses c ON s.id = c.nope WHERE grade = 'high'",
            "sqlite", "students with courses", ["students", "courses"], None,
        )
        # C1 不触发(表都在)且 C2/C3 因缺 schema 静默
        assert out == []

    # ── 工具级集成:警告为软信号,不阻塞 ──
    async def test_validate_tool_appends_warnings_without_blocking(self, sqlite_registry):
        tools, handlers, _ = make_sql_tools(
            sqlite_registry, "How many students are there in total?", "en", "sqlite",
            matched_tables=["students"],
        )
        text = await handlers["validate_sql"](
            {"sql": "SELECT COUNT(*) FROM pupils"},
        )
        assert text.startswith("valid")
        assert "WARNINGS" in text and "'students'" in text
        # 合规 SQL → 无警告,保持原契约
        assert await handlers["validate_sql"](
            {"sql": "SELECT COUNT(*) FROM students"},
        ) == "valid"


class TestSearchValues:
    """search_values 值检索工具:单列/扫描/转义/错误折叠。"""

    async def test_single_column_case_insensitive_hit(self, sqlite_registry):
        out = await search_values(sqlite_registry, "students", "ala", column="county")
        data = json.loads(out)
        assert data["ok"] and data["values"] == ["Alameda"]

    async def test_single_column_no_match(self, sqlite_registry):
        out = await search_values(sqlite_registry, "students", "zzzz", column="county")
        data = json.loads(out)
        assert data["ok"] and data["values"] == []

    async def test_scan_locates_column_with_hits(self, sqlite_registry):
        """不指定列 → 扫描前 N 列,返回 column → 匹配值 映射('los' 应命中 county 的 'Los Angeles')。"""
        out = await search_values(sqlite_registry, "students", "los")
        data = json.loads(out)
        assert data["ok"]
        assert data["hits"].get("county") == ["Los Angeles"]

    async def test_scan_honest_empty(self, sqlite_registry):
        out = await search_values(sqlite_registry, "students", "zzzz")
        data = json.loads(out)
        assert data["ok"] and data["hits"] == {}
        assert "no column contains" in data.get("note", "")

    async def test_wildcards_treated_literally(self, sqlite_registry):
        """%,_ 按字面匹配:数据里没有含 '%' 的值 → 空命中,且不因模式报错。"""
        assert _like_pattern("100%") == "%100!%%"
        assert _like_pattern("a_b") == "%a!_b%"
        out = await search_values(sqlite_registry, "students", "100%")
        data = json.loads(out)
        assert data["ok"] and data["hits"] == {}

    async def test_unknown_table_and_column_fold_to_error(self, sqlite_registry):
        out = await search_values(sqlite_registry, "missing", "x")
        assert json.loads(out)["ok"] is False
        out = await search_values(sqlite_registry, "students", "x", column="nope")
        assert json.loads(out)["ok"] is False

    async def test_no_connectors_folds_to_error(self):
        out = await search_values(None, "students", "x")
        assert json.loads(out)["ok"] is False

    async def test_scan_propagates_error_when_all_columns_fail(
        self, sqlite_registry, monkeypatch,
    ):
        """扫描路径不得把列级错误谎报成'无匹配'——首错上抛。"""
        async def fail(connectors, table, column, keyword, timeout_s):
            return {"ok": False, "error": f"boom on {column}"}
        monkeypatch.setattr(
            "trove.workflow.nodes.gen_sql._search_one", fail,
        )
        out = await search_values(sqlite_registry, "students", "x")
        data = json.loads(out)
        assert data["ok"] is False
        assert data["error"] == "boom on id"  # 首列首错


class TestReflectAdaptiveSkip:
    """自适应减负:快径命中 / 规则全过+低复杂度 → 跳过 LLM 裁决。"""

    async def test_fast_path_skips_judge(self):
        class NoCallLLM:
            async def chat(self, *a, **k):
                raise AssertionError("LLM must not be called for fast path")

        node = make_reflect(NoCallLLM(), AgentConfig(target="mock/model"))
        update = await node(make_state(
            fast_path=True, sql="SELECT COUNT(*) FROM students",
            row_count=1, columns=["count"], rows=[[5]],
        ))
        assert update["verdict"] == "OK"
        assert update["reason"] == "fast path deterministic template match (kb init)"

    async def test_rules_passed_simple_skips_judge(self):
        class NoCallLLM:
            async def chat(self, *a, **k):
                raise AssertionError("LLM must not be called for simple pass")

        node = make_reflect(NoCallLLM(), AgentConfig(target="mock/model"))
        update = await node(make_state(
            rules_passed=True, complexity="simple",
            sql="SELECT COUNT(*) AS n FROM students",
            row_count=1, columns=["n"], rows=[[1]],
        ))
        assert update["verdict"] == "OK"
        assert update["reason"] == "deterministic rules passed; reflect skipped"

    async def test_reflect_skip_all_with_standard_complexity(self):
        class NoCallLLM:
            async def chat(self, *a, **k):
                raise AssertionError("LLM must not be called with skip=all")

        node = make_reflect(NoCallLLM(), AgentConfig(target="mock/model", reflect_skip="all"))
        update = await node(make_state(
            rules_passed=True, complexity="standard",
            sql="SELECT COUNT(*) AS n FROM students",
            row_count=2, columns=["n"], rows=[[1], [2]],
        ))
        assert update["verdict"] == "OK"

    async def test_reflect_skip_standard_covers_standard_complexity(self):
        """reflect_skip=standard:规则全过 → simple/standard 都跳过裁决。"""
        class NoCallLLM:
            async def chat(self, *a, **k):
                raise AssertionError("LLM must not be called with skip=standard")

        node = make_reflect(NoCallLLM(), AgentConfig(target="mock/model", reflect_skip="standard"))
        update = await node(make_state(
            rules_passed=True, complexity="standard",
            sql="SELECT COUNT(*) AS n FROM students",
            row_count=2, columns=["n"], rows=[[1], [2]],
        ))
        assert update["verdict"] == "OK"
        assert update["reason"] == "deterministic rules passed; reflect skipped"

    async def test_reflect_skip_simple_does_not_cover_standard(self):
        """默认 simple:standard 复杂度的规则全过仍交给 LLM 裁决。"""
        class LLM:
            def __init__(self):
                self.called = False

            async def chat(self, *a, **k):
                self.called = True
                return "RETRY: 结果与问题语义不符"

        llm = LLM()
        node = make_reflect(llm, AgentConfig(target="mock/model", reflect_skip="simple"))
        update = await node(make_state(
            rules_passed=True, complexity="standard",
            sql="SELECT COUNT(*) AS n FROM students",
            row_count=2, columns=["n"], rows=[[1], [2]],
        ))
        assert llm.called is True

    async def test_reflect_skip_off_keeps_judge(self):
        class LLM:
            async def chat(self, *a, **k):
                return "OK"

        node = make_reflect(LLM(), AgentConfig(target="mock/model", reflect_skip="off"))
        update = await node(make_state(
            rules_passed=True, complexity="simple",
            sql="SELECT COUNT(*) AS n FROM students",
            row_count=2, columns=["n"], rows=[[1], [2]],
        ))
        assert update["verdict"] == "OK"

    async def test_weak_signal_keeps_judge(self):
        """metadata 倾向题保留 NO_SQL 出口,规则全过也不跳过。"""
        class LLM:
            async def chat(self, *a, **k):
                return "NO_SQL"

        node = make_reflect(LLM(), AgentConfig(target="mock/model"))
        update = await node(make_state(
            rules_passed=True, complexity="simple",
            sql="SELECT COUNT(*) AS n FROM students",
            row_count=1, columns=["n"], rows=[[1]],
            question="disp 表是啥",
        ))
        assert update["verdict"] == "NO_SQL"

    async def test_rules_not_passed_keeps_judge(self):
        class LLM:
            async def chat(self, *a, **k):
                return "OK"

        node = make_reflect(LLM(), AgentConfig(target="mock/model"))
        update = await node(make_state(
            rules_passed=False, complexity="simple",
            row_count=2, columns=["n"], rows=[[1], [2]],
        ))
        assert update["verdict"] == "OK"


class RecordingLLM:
    """记录每次调用的 (model, messages),返回固定响应 —— 模型分层断言用。"""

    def __init__(self, response: str = "OK"):
        self._response = response
        self.calls: list[tuple[str, list]] = []

    async def chat(self, model, messages, **kwargs):
        self.calls.append((model, messages))
        return self._response


class TestModelTiering:
    """模型分层:simple/standard → model_fast,complex → target。"""

    async def test_generate_tiers_by_complexity(self):
        from trove.workflow.state import GenSQLState

        config = AgentConfig(target="mock/target", model_fast="mock/fast")
        for complexity, expected in [("simple", "mock/fast"), ("standard", "mock/fast"),
                                     ("complex", "mock/target")]:
            llm = RecordingLLM()
            node = make_generate(llm, config)
            state = GenSQLState(question="q", complexity=complexity, dialect="sqlite")
            await node(state)
            assert llm.calls[-1][0] == expected, complexity

    async def test_generate_model_fast_empty_keeps_target(self):
        from trove.workflow.state import GenSQLState

        config = AgentConfig(target="mock/target")
        llm = RecordingLLM()
        node = make_generate(llm, config)
        await node(GenSQLState(question="q", complexity="simple", dialect="sqlite"))
        assert llm.calls[-1][0] == "mock/target"

    async def test_reflect_tiers_by_complexity(self):
        config = AgentConfig(target="mock/target", model_fast="mock/fast")
        for complexity, expected in [("simple", "mock/fast"), ("standard", "mock/fast"),
                                     ("complex", "mock/target")]:
            llm = RecordingLLM()
            node = make_reflect(llm, config)
            update = await node(make_state(
                rules_passed=False, complexity=complexity,
                sql="SELECT COUNT(*) FROM students",
                row_count=1, columns=["n"], rows=[[1]],
            ))
            assert update["verdict"] == "OK"
            assert llm.calls[-1][0] == expected, complexity

    async def test_semantics_tiers_by_complexity(self):
        from trove.workflow.nodes.semantics import make_semantics

        config = AgentConfig(target="mock/target", model_fast="mock/fast",
                             explain_semantics=True)
        for complexity, expected in [("simple", "mock/fast"), ("complex", "mock/target")]:
            llm = RecordingLLM()
            node = make_semantics(llm, config)
            update = await node(make_state(
                complexity=complexity, sql="SELECT COUNT(*) FROM students",
            ))
            assert "semantics" in update
            assert llm.calls[-1][0] == expected, complexity

    async def test_insights_tiers_by_complexity(self):
        from trove.workflow.nodes.insights import make_insights

        config = AgentConfig(target="mock/target", model_fast="mock/fast", insights=True)
        for complexity, expected in [("simple", "mock/fast"), ("complex", "mock/target")]:
            llm = RecordingLLM()
            node = make_insights(llm, config)
            update = await node(make_state(
                complexity=complexity, sql="SELECT COUNT(*) FROM students",
                row_count=1, columns=["n"], rows=[[5]],
            ))
            assert "insights" in update
            assert llm.calls[-1][0] == expected, complexity


class TestReflectProjectionSelfCheck:
    """投影宽度自洽是 skip 的必要条件(结果列数必须等于 SQL SELECT 列数)。"""

    async def test_skip_blocked_on_projection_mismatch(self):
        """SQL 选 2 列但结果只有 1 列 → 不跳过,交 LLM 法官。"""
        class LLM:
            def __init__(self):
                self.called = False

            async def chat(self, *a, **k):
                self.called = True
                return "OK"

        llm = LLM()
        node = make_reflect(llm, AgentConfig(target="mock/model"))
        update = await node(make_state(
            rules_passed=True, complexity="simple",
            sql="SELECT name, grade FROM students",
            row_count=2, columns=["name"], rows=[["a"]],
        ))
        assert llm.called is True
        assert update["verdict"] == "OK"

    async def test_skip_pass_on_projection_match(self):
        class NoCallLLM:
            async def chat(self, *a, **k):
                raise AssertionError("LLM must not be called")

        node = make_reflect(NoCallLLM(), AgentConfig(target="mock/model"))
        update = await node(make_state(
            rules_passed=True, complexity="simple",
            sql="SELECT name, grade FROM students",
            row_count=2, columns=["name", "grade"], rows=[["a", 1]],
        ))
        assert update["verdict"] == "OK"
        assert update["reason"] == "deterministic rules passed; reflect skipped"

    async def test_select_star_unverifiable_keeps_judge(self):
        """SELECT * 宽度不可验证 → 不跳过。"""
        class LLM:
            def __init__(self):
                self.called = False

            async def chat(self, *a, **k):
                self.called = True
                return "OK"

        llm = LLM()
        node = make_reflect(llm, AgentConfig(target="mock/model"))
        update = await node(make_state(
            rules_passed=True, complexity="simple",
            sql="SELECT * FROM students",
            row_count=2, columns=["a", "b"], rows=[[1, 2]],
        ))
        assert llm.called is True
        assert update["verdict"] == "OK"

    async def test_zero_rows_weak_signal_keeps_judge(self):
        """0 行 + 弱信号:EMPTY 分支被 has_weak_signal 拦下,skip 也被 row_count>0 拦下。"""
        class LLM:
            async def chat(self, *a, **k):
                return "NO_SQL"

        node = make_reflect(LLM(), AgentConfig(target="mock/model"))
        update = await node(make_state(
            rules_passed=True, complexity="simple",
            sql="SELECT COUNT(*) AS n FROM students",
            row_count=0, columns=["n"], rows=[],
            question="disp 表是啥",
        ))
        assert update["verdict"] == "NO_SQL"


class TestProjectionWidthHelper:
    def test_plain_select(self):
        from trove.workflow.nodes.reflect import _projection_width_matches

        assert _projection_width_matches("SELECT a, b FROM t", "sqlite", 2) is True
        assert _projection_width_matches("SELECT a, b FROM t", "sqlite", 3) is False

    def test_aggregate_alias(self):
        from trove.workflow.nodes.reflect import _projection_width_matches

        assert _projection_width_matches(
            "SELECT COUNT(*) AS n FROM t", "sqlite", 1,
        ) is True

    def test_select_star_false(self):
        from trove.workflow.nodes.reflect import _projection_width_matches

        assert _projection_width_matches("SELECT * FROM t", "sqlite", 5) is False

    def test_cte_uses_outer_select(self):
        from trove.workflow.nodes.reflect import _projection_width_matches

        sql = "WITH x AS (SELECT a FROM t) SELECT a, b FROM x"
        assert _projection_width_matches(sql, "sqlite", 2) is True

    def test_union_uses_first_select(self):
        from trove.workflow.nodes.reflect import _projection_width_matches

        sql = "SELECT a, b FROM t UNION SELECT c, d FROM u"
        assert _projection_width_matches(sql, "sqlite", 2) is True

    def test_parse_failure_false(self):
        from trove.workflow.nodes.reflect import _projection_width_matches

        assert _projection_width_matches("NOT SQL AT ALL", "sqlite", 2) is False
        assert _projection_width_matches("", "sqlite", 2) is False

    def test_non_query_false(self):
        from trove.workflow.nodes.reflect import _projection_width_matches

        assert _projection_width_matches("INSERT INTO t VALUES (1)", "sqlite", 1) is False

    def test_zero_or_negative_columns_false(self):
        from trove.workflow.nodes.reflect import _projection_width_matches

        assert _projection_width_matches("SELECT a FROM t", "sqlite", 0) is False
        assert _projection_width_matches("SELECT a FROM t", "sqlite", -1) is False
