"""LangGraph topology tests: subgraph retry loop, reflect loop, degradation."""

import pytest

from trove.core.config import AgentConfig
from trove.workflow.state import WorkflowState, GenSQLState
from trove.workflow import graphs as graphs_module
from trove.workflow.graphs import GraphServices, build_graphs, build_gen_sql_subgraph

VALID_SQL = "```sql\nSELECT name FROM students;\n```"
INVALID_SQL = "```sql\nSELEC * FROM students;\n```"


class RecordingLLM:
    """Returns scripted responses; IndexError (→ graph error) if called too often."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # each entry: list of message dicts

    async def chat(self, model, messages, **kwargs):
        self.calls.append(messages)
        return self._responses.pop(0)


def make_services(llm, catalog=None, connectors=None, kb=None):
    return GraphServices(
        llm=llm,
        catalog=catalog,
        connectors=connectors,
        config=AgentConfig(target="mock/model"),
        kb=kb,
    )


def make_state(**kwargs):
    defaults = {"session_id": "s1", "question": "Average grade by county"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


def build(services, multi_candidate=False, planner=False, clarify=False):
    """Build graphs with single-candidate generation (scripted-response tests)."""
    return build_graphs(services, multi_candidate=multi_candidate, planner=planner, clarify=clarify)


# ── gen_sql subgraph ─────────────────────────────────────


class TestGenSQLSubgraph:
    async def test_single_valid_generation(self):
        sub = build_gen_sql_subgraph(make_services(RecordingLLM([VALID_SQL])))
        out = await sub.ainvoke(
            GenSQLState(question="q", schema_context="", dialect="sqlite")
        )
        assert out["sql"] == "SELECT name FROM students;"
        assert out["error"] == ""

    async def test_retries_with_fix_prompt_then_succeeds(self):
        llm = RecordingLLM([INVALID_SQL, INVALID_SQL, VALID_SQL])
        sub = build_gen_sql_subgraph(make_services(llm), max_retries=3)
        out = await sub.ainvoke(
            GenSQLState(question="q", schema_context="", dialect="sqlite")
        )
        assert out["sql"] == "SELECT name FROM students;"
        assert out["error"] == ""
        assert len(llm.calls) == 3
        # Second and third calls used the fix prompt
        assert "failed validation" in llm.calls[1][-1]["content"]
        assert "failed validation" in llm.calls[2][-1]["content"]

    async def test_exhaustion_sets_error(self):
        llm = RecordingLLM([INVALID_SQL] * 3)
        sub = build_gen_sql_subgraph(make_services(llm), max_retries=3)
        out = await sub.ainvoke(
            GenSQLState(question="q", schema_context="", dialect="sqlite")
        )
        assert out["error"]
        assert "3 attempts" in out["error"]

    async def test_preexisting_error_skips_loop(self):
        sub = build_gen_sql_subgraph(make_services(RecordingLLM([])))
        out = await sub.ainvoke(
            GenSQLState(question="q", schema_context="", dialect="sqlite", error="upstream")
        )
        assert out["error"] == "upstream"


# ── Main graphs ──────────────────────────────────────────


class TestReflectionGraph:
    async def test_happy_path(self, sqlite_registry, catalog):
        llm = RecordingLLM([VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["verdict"] == "OK"
        assert final["retry_count"] == 0
        assert final["error"] == ""
        assert "SELECT name FROM students;" in final["sql"]
        assert final["row_count"] == 5
        assert "## Answer" in final["final_response"]
        assert len(llm.calls) == 2  # gen_sql + reflect

    async def test_retry_loop_regenerates_with_reason(self, sqlite_registry, catalog):
        llm = RecordingLLM([VALID_SQL, "RETRY: wrong grouping", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["verdict"] == "OK"
        assert final["retry_count"] == 1
        assert final["error"] == ""
        # The regenerated SQL prompt carried the reflect reason
        assert "wrong grouping" in llm.calls[2][-1]["content"]

    async def test_retry_cap_forces_accept(self, sqlite_registry, catalog, monkeypatch):
        monkeypatch.setattr(graphs_module, "MAX_REFLECT_RETRIES", 2)
        llm = RecordingLLM([
            VALID_SQL, "RETRY: a", VALID_SQL, "RETRY: b", VALID_SQL, "RETRY: c",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["retry_count"] == 2
        assert final["verdict"] == "OK"
        assert final["error"] == ""
        assert len(llm.calls) == 6  # 3 generate passes + 3 reflect calls

    async def test_gen_sql_exhaustion_degrades_to_output(self, sqlite_registry, catalog):
        """gen_sql subgraph exhausts retries → execute/reflect skipped → error section."""
        llm = RecordingLLM([INVALID_SQL] * 3)
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert "3 attempts" in final["error"]
        assert final["row_count"] == -1  # execute_sql never ran
        assert "**Error**" in final["final_response"]
        assert len(llm.calls) == 3  # reflect never called

    async def test_execute_failure_degrades_to_output(self, sqlite_registry, catalog, monkeypatch):
        """执行失败 → 修正预算内重生成 → 耗尽后优雅降级（不再首错即降级）。"""
        monkeypatch.setattr(graphs_module, "MAX_REFLECT_RETRIES", 2)
        bad_sql = "```sql\nSELECT * FROM nonexistent;\n```"
        llm = RecordingLLM([bad_sql, bad_sql, bad_sql])  # 初稿 + 2 轮修正
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"]
        assert final["retry_count"] == 2
        assert "**Error**" in final["final_response"]
        assert final["verdict"] == ""  # reflect skipped

    async def test_execute_error_feedback_fixes_sql(self, sqlite_registry, catalog):
        """执行错误反馈给 gen_sql → 修正后成功（修正闭环）。"""
        llm = RecordingLLM([
            "```sql\nSELECT * FROM nonexistent;\n```",           # 初稿（运行时错误）
            "```sql\nSELECT name FROM students;\n```",           # 修正稿
            "OK",                                                # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["error_feedback"] == ""
        assert final["retry_count"] == 1
        assert final["row_count"] == 5
        assert final["verdict"] == "OK"
        # 修正 prompt 携带了执行错误信息
        assert "nonexistent" in llm.calls[1][-1]["content"]

    async def test_preexisting_error_passes_straight_to_output(self, sqlite_registry, catalog):
        graphs = build(make_services(RecordingLLM([]), catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state(error="upstream failed"))
        assert "upstream failed" in final["final_response"]
        assert final["row_count"] == -1

    async def test_empty_result_reflect_short_circuits(self, sqlite_registry, catalog):
        """Zero rows → EMPTY verdict, no reflect LLM call."""
        llm = RecordingLLM(["```sql\nSELECT name FROM students WHERE 0;\n```"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["verdict"] == "EMPTY"
        assert len(llm.calls) == 1  # only gen_sql


class TestIntentRouting:
    async def test_schema_intent_answers_without_llm(self, sqlite_registry, catalog):
        """「有哪些表」→ 直接给表清单，不调 LLM 生成。"""
        llm = RecordingLLM([])  # 任何调用都会 IndexError
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state(question="有哪些表"))
        assert final["intent"] == "metadata"
        assert "students" in final["intent_answer"]
        assert final["sql"] == ""

    async def test_lineage_intent_lists_joins(self, sqlite_registry, catalog):
        """血缘意图 → 关联关系清单。"""
        adapter = await sqlite_registry.get()
        await adapter.execute(
            "CREATE TABLE district (district_id INTEGER PRIMARY KEY, name TEXT)"
        )
        await adapter.execute(
            "CREATE TABLE city (city_id INTEGER PRIMARY KEY, district_id INTEGER)"
        )
        llm = RecordingLLM([])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="city 表的血缘")
        )
        assert final["intent"] == "metadata"
        assert "city.district_id → district.district_id" in final["intent_answer"]

    async def test_semantic_intent_uses_kb(self, sqlite_registry, catalog, tmp_path):
        """语义意图 → 术语口径查询知识库。"""
        from trove.services.kb.service import KbService

        kb = KbService(tmp_path / "proj")
        ds_dir = kb.kb_dir / sqlite_registry.default_name
        ds_dir.mkdir(parents=True)
        (ds_dir / "semantics.yml").write_text(
            "terms:\n  - term: 平均成绩\n    mapping: AVG(students.grade)\n"
            "    tables: [students]\n    definition: 学生平均分\n",
            encoding="utf-8",
        )
        llm = RecordingLLM([])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=kb))
        final = await graphs["reflection"].ainvoke(
            make_state(question="平均成绩的定义")
        )
        assert final["intent"] == "metadata"
        assert "AVG(students.grade)" in final["intent_answer"]

    async def test_query_intent_still_runs_pipeline(self, sqlite_registry, catalog):
        """普通查询问题照常走生成流水线。"""
        llm = RecordingLLM([VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["intent"] == "query"
        assert final["row_count"] == 5

    async def test_schema_intent_answers_in_english(self, sqlite_registry, catalog):
        """英文问题 → 英文答案。"""
        llm = RecordingLLM([])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state(question="list tables"))
        assert final["intent"] == "metadata"
        assert "tables" in final["intent_answer"].lower()
        assert "数据源" not in final["intent_answer"]


class TestIntentLLMFallback:
    async def test_weak_signal_llm_classifies_metadata(self, sqlite_registry, catalog):
        """弱信号（裸「表」字）→ LLM 二分类确认 metadata → 表详情。"""
        llm = RecordingLLM(["metadata"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="students 表有什么")
        )
        assert final["intent"] == "metadata"
        assert "id" in final["intent_answer"]
        assert "INTEGER" in final["intent_answer"]
        assert len(llm.calls) == 1  # 仅意图分类，无生成

    async def test_weak_signal_llm_garbage_falls_back_to_query(self, sqlite_registry, catalog):
        """LLM 分类不可解析 → query 兜底，正常走生成流水线。"""
        llm = RecordingLLM(["blah blah", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="students 表有什么")
        )
        assert final["intent"] == "query"
        assert final["row_count"] == 5

    async def test_composite_metadata_question(self, sqlite_registry, catalog):
        """复合问题（含义 + 关系）→ 表详情与关联关系同时输出。"""
        adapter = await sqlite_registry.get()
        await adapter.execute(
            "CREATE TABLE courses (course_id INTEGER PRIMARY KEY, students_id INTEGER, title TEXT)"
        )
        llm = RecordingLLM([])  # 强信号（含义/关系），不调 LLM
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="students 和 courses 表分别什么含义？有什么关系")
        )
        assert final["intent"] == "metadata"
        assert "students" in final["intent_answer"]
        assert "courses" in final["intent_answer"]          # 两张表详情都有
        assert "courses.students_id → students.id" in final["intent_answer"]  # 关系段


class TestClarifyRouting:
    async def test_no_table_match_asks_user(self, sqlite_registry, catalog):
        """clarify 开启时：无表匹配 → 反问用户，不调用 LLM 生成。"""
        llm = RecordingLLM([])  # 任何调用都会 IndexError
        graphs = build(make_services(llm, catalog, sqlite_registry), clarify=True)
        final = await graphs["reflection"].ainvoke(
            make_state(question="zzz 完全不相关的数据")
        )
        assert final["clarification_question"]
        assert "Clarification" in final["final_response"]
        assert final["sql"] == ""

    async def test_default_is_permissive_without_match(self, sqlite_registry, catalog):
        """默认（clarify 关闭）：无表匹配也照常生成，不拦截。"""
        llm = RecordingLLM([VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="zzz 完全不相关的数据")
        )
        assert final["clarification_question"] == ""
        assert final["sql"] == "SELECT name FROM students;"
        assert final["row_count"] == 5

    async def test_matched_tables_proceed_normally(self, sqlite_registry, catalog):
        """有表匹配 → 正常走生成流程。"""
        llm = RecordingLLM(["plan: use students", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry), planner=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["clarification_question"] == ""
        assert final["row_count"] == 5
        # 查询计划到达了 gen_sql 的生成 prompt
        assert "Query plan" in llm.calls[1][-1]["content"]


class TestFixedGraph:
    async def test_no_reflection(self, sqlite_registry, catalog):
        llm = RecordingLLM([VALID_SQL])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["fixed"].ainvoke(make_state())
        assert final["verdict"] == ""  # reflect never ran
        assert final["row_count"] == 5
        assert "## Answer" in final["final_response"]
        assert len(llm.calls) == 1

    async def test_execute_error_feedback_retries(self, sqlite_registry, catalog):
        """fixed 图同样带执行错误修正闭环。"""
        llm = RecordingLLM([
            "```sql\nSELECT * FROM nonexistent;\n```",
            "```sql\nSELECT name FROM students;\n```",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["fixed"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["row_count"] == 5
        assert len(llm.calls) == 2


class TestEmptyGraph:
    async def test_pass_through(self):
        graphs = build(make_services(RecordingLLM([])))
        final = await graphs["empty"].ainvoke(make_state())
        assert "(No query executed)" in final["final_response"]


class TestKnowledgeBaseGraph:
    def _kb(self, tmp_path, datasource):
        from trove.services.kb.service import KbService

        kb = KbService(tmp_path / "proj")
        ds_dir = kb.kb_dir / datasource
        ds_dir.mkdir(parents=True)
        (ds_dir / "semantics.yml").write_text(
            """
terms:
  - term: 平均成绩
    aliases: []
    mapping: AVG(students.grade)
    tables: [students]
    definition: 学生平均分
""",
            encoding="utf-8",
        )
        (ds_dir / "schema_notes.yml").write_text(
            """
tables:
  - name: students
    description: 学生成绩表
    columns:
      - name: grade
        description: 考试成绩
""",
            encoding="utf-8",
        )
        (ds_dir / "examples.yml").write_text(
            """
examples:
  - question: 各地区平均成绩是多少
    sql: SELECT county, AVG(grade) FROM students GROUP BY county
    tags: [成绩, 地区]
""",
            encoding="utf-8",
        )
        return kb

    async def test_chinese_question_end_to_end(self, sqlite_registry, catalog, tmp_path):
        """中文问题：术语命中表 + 示例注入 prompt + kb_hits 贯穿状态。"""
        kb = self._kb(tmp_path, sqlite_registry.default_name)
        llm = RecordingLLM([
            "```sql\nSELECT county, AVG(grade) FROM students GROUP BY county;\n```",
            "OK",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=kb))
        final = await graphs["reflection"].ainvoke(
            make_state(question="学生们的平均成绩是多少")
        )

        assert "students" in final["matched_tables"]
        kinds = {h.get("kind") for h in final["kb_hits"]}
        assert kinds == {"term", "example"}
        assert final["row_count"] > 0
        assert final["error"] == ""
        assert "Knowledge base" in final["final_response"]
        # The reference example reached the LLM prompt
        assert any(
            "Reference examples" in " ".join(
                str(m.get("content", "")) for m in call
            )
            for call in llm.calls
        )

    async def test_no_kb_has_no_kb_hits(self, sqlite_registry, catalog):
        """kb 未配置时状态与行为不变（回归）。"""
        llm = RecordingLLM([VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["kb_hits"] == []
        assert "Knowledge base" not in final["final_response"]

    async def test_data_source_rules_reach_prompt(self, sqlite_registry, catalog, tmp_path):
        """rules.yml 的业务规则注入生成 prompt。"""
        from trove.services.kb.service import KbService

        kb = KbService(tmp_path / "proj")
        ds_dir = kb.kb_dir / sqlite_registry.default_name
        ds_dir.mkdir(parents=True)
        (ds_dir / "rules.yml").write_text(
            "rules:\n  - rule: '年龄 = 1998 - YEAR(birth_date)'\n",
            encoding="utf-8",
        )
        llm = RecordingLLM([VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=kb))
        await graphs["reflection"].ainvoke(make_state())
        assert "Data source rules" in llm.calls[0][-1]["content"]

    async def test_context_budget_drops_low_priority_blocks(self, sqlite_registry, catalog, monkeypatch):
        """预算不足时低优先级块（history）被排除，核心 schema 保留。"""
        monkeypatch.setattr(graphs_module, "CONTEXT_BUDGET_TOKENS", 5)
        llm = RecordingLLM([VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(history="user: 平均成绩是多少\nassistant: 85 分")
        )
        assert final["error"] == ""
        gen_prompt = llm.calls[0][-1]["content"]
        assert "Conversation history" not in gen_prompt  # 低优先级被预算排除
        assert "Database schema" in gen_prompt                 # 核心保留
        usage = final["context_usage"]
        assert any(u["name"] == "history" and not u["included"] for u in usage)

    async def test_history_reaches_generation_prompt(self, sqlite_registry, catalog):
        """会话历史注入 gen_sql 的生成 prompt。"""
        llm = RecordingLLM([VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        await graphs["reflection"].ainvoke(
            make_state(history="user: 平均成绩是多少\nassistant: 85 分")
        )
        assert "平均成绩是多少" in llm.calls[0][-1]["content"]

    async def test_validation_rule_fixes_count_question(self, sqlite_registry, catalog):
        """count 问题先返回多行 → 规则失败 → 带理由重新生成 → 修正成功。"""
        llm = RecordingLLM([
            "```sql\nSELECT name FROM students;\n```",   # 多行（规则失败）
            "```sql\nSELECT COUNT(*) FROM students;\n```",  # 修正：单值
            "OK",                                        # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="how many students are there")
        )
        assert final["error"] == ""
        assert final["row_count"] == 1
        assert final["retry_count"] == 1
        assert "Validation rule" in llm.calls[1][-1]["content"]

    async def test_validation_rule_fixes_empty_list_question(self, sqlite_registry, catalog):
        """list 问题返回 0 行 → 规则触发重新生成 → 非空结果。"""
        llm = RecordingLLM([
            "```sql\nSELECT name FROM students WHERE 0;\n```",
            "```sql\nSELECT name FROM students;\n```",
            "OK",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="list all students")
        )
        assert final["row_count"] == 5
        assert final["retry_count"] == 1

    async def test_two_candidates_consensus_passes(self, sqlite_registry, catalog):
        """两个候选结果一致 → 高置信放行（无需修正）。"""
        llm = RecordingLLM([
            VALID_SQL,                                             # 主候选
            "```sql\nSELECT name FROM students ORDER BY name;\n```",  # 副候选（同结果）
            "OK",                                                  # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), multi_candidate=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["retry_count"] == 0
        assert final["row_count"] == 5
        assert len(final["candidates"]) == 1
        assert len(llm.calls) == 3  # 主 + 副 + reflect

    async def test_candidate_disagreement_regenerates(self, sqlite_registry, catalog):
        """候选结果不一致 → 反馈重生成 → 一致后放行。"""
        llm = RecordingLLM([
            VALID_SQL,                                             # pass1 主
            "```sql\nSELECT name FROM students WHERE 0;\n```",     # pass1 副（0 行）
            VALID_SQL,                                             # pass2 主
            "```sql\nSELECT name FROM students ORDER BY name;\n```",  # pass2 副
            "OK",                                                  # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), multi_candidate=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["retry_count"] == 1
        assert final["row_count"] == 5
        # pass2 的生成 prompt 带了一致性失败理由
        assert "different results" in llm.calls[2][-1]["content"]
