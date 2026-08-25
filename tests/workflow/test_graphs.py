"""LangGraph topology tests: subgraph retry loop, reflect loop, degradation."""

import asyncio
from types import SimpleNamespace

import pytest

from trove.core.config import AgentConfig
from trove.workflow.state import WorkflowState, GenSQLState
from trove.workflow import graphs as graphs_module
from trove.workflow.graphs import (
    GraphServices,
    _candidate_schedule,
    build_gen_sql_subgraph,
    build_graphs,
)

VALID_SQL = "```sql\nSELECT name FROM students;\n```"
INVALID_SQL = "```sql\nSELEC * FROM students;\n```"


def test_candidate_schedule_default_matches_historical():
    """scaling=5 必须逐字节等于历史 4 温度子图(0.3/0.5/0.7/1.0,无风格)。"""
    assert _candidate_schedule(4) == [(0.3, ""), (0.5, ""), (0.7, ""), (1.0, "")]


def test_candidate_schedule_scales_deterministically():
    """大池(49/199):温度单调铺开 + 风格轮换,确定性生成(同输入同输出)。"""
    s49 = _candidate_schedule(49)
    assert len(s49) == 49
    temps = [t for t, _ in s49]
    assert temps == sorted(temps)
    assert 0.2 <= temps[0] and temps[-1] <= 1.2
    assert len({round(t, 1) for t in temps}) >= 10  # 温度真实铺开,没有挤成一团
    modes = [m for _, m in s49]
    assert set(modes) == {"", "cte", "explicit-join", "subquery"}
    assert _candidate_schedule(49) == s49  # 确定性:两次构建一致
    assert len(_candidate_schedule(199)) == 199


class RecordingLLM:
    """Returns scripted responses; IndexError (→ graph error) if called too often.

    chat_full consumes the same queue and returns a content-only message
    (no tool calls) — the agentic nodes then behave like classic
    single-shot generation for existing scripted tests.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # each entry: list of message dicts

    async def chat(self, model, messages, **kwargs):
        self.calls.append(messages)
        return self._responses.pop(0)

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return {"content": self._responses.pop(0), "tool_calls": []}


def make_services(llm, catalog=None, connectors=None, kb=None, config=None,
                   semantic_layer=None):
    if semantic_layer is None and connectors is not None:
        # 语义优先(Phase B):默认注入 fixture 的确定性语义模型
        semantic_layer = getattr(connectors, "_test_semantic_provider", None)
    return GraphServices(
        llm=llm,
        catalog=catalog,
        connectors=connectors,
        config=config or AgentConfig(target="mock/model"),
        kb=kb,
        semantic_layer=semantic_layer,
    )


def make_state(**kwargs):
    defaults = {"session_id": "s1", "question": "Average grade by county"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


def build(services, multi_candidate=False, planner=False, clarify=False, agentic=False):
    """Build graphs with single-candidate classic generation (scripted-response tests)."""
    return build_graphs(services, multi_candidate=multi_candidate, planner=planner,
                        clarify=clarify, agentic=agentic)


# ── gen_sql subgraph ─────────────────────────────────────


class TestGenSQLSemanticLayerTerms:
    """实时语义层 metric 以 term 形态合并进 gen_sql 提示词(KB 优先去重)。"""

    OSSIE_SAMPLE = """
semantic_model:
  - name: financial_analytics
    datasets:
      - name: loan
        source: financial.loan
    metrics:
      - name: total_loan_amount
        description: Total amount of all loans
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(loan.amount)
        ai_context:
          synonyms:
            - "total loans"
    """

    async def test_live_terms_merged_into_gen_prompt(self, tmp_path, demo_registry):
        from trove.services.semantic_layer.provider import SemanticLayerProvider
        semantic_dir = tmp_path / "semantic" / "demo"
        semantic_dir.mkdir(parents=True)
        (semantic_dir / "model.yml").write_text(self.OSSIE_SAMPLE)

        provider = SemanticLayerProvider(semantic_dir, "demo")
        llm = RecordingLLM(["```sql\nSELECT SUM(amount) FROM loan;\n```"])
        sub = build_gen_sql_subgraph(make_services(llm))
        services = make_services(llm, connectors=demo_registry, semantic_layer=provider)
        node = graphs_module._make_gen_sql_node(services, sub)
        state = make_state(
            question="What is the total loans volume?",
            matched_tables=["loan"],
        )
        out = await node(state)
        assert out["sql"]
        prompt = " ".join(str(m.get("content", "")) for m in llm.calls[-1])
        # 同义词命中 → live term(名字 + mapping)注入 Terminology 段
        assert "total_loan_amount" in prompt
        assert "SUM(loan.amount)" in prompt
        assert "Terminology" in prompt

    async def test_kb_terms_take_precedence_on_duplicate_names(self, tmp_path, demo_registry):
        """同名 term:KB 在先在先,实时语义层同名不覆盖。"""
        from trove.services.kb.service import KbService, TermHit
        from trove.services.semantic_layer.provider import SemanticLayerProvider

        # KB:同名 term,映射不同(KB 口径优先)
        from tests.helpers.kb import ossie_semantics_yaml

        kb = KbService(tmp_path / "proj")
        ds_dir = kb.kb_dir / "demo"
        ds_dir.mkdir(parents=True)
        (ds_dir / "semantics.yml").write_text(ossie_semantics_yaml([
            {"term": "total_loan_amount", "aliases": ["total loans"],
             "mapping": "SUM(loan.amount) * 2", "tables": ["loan"]},
        ]))
        await kb.force_sync(default_datasource="demo")

        # 实时语义层:同名但映射为普通 SUM
        semantic_dir = tmp_path / "semantic" / "demo"
        semantic_dir.mkdir(parents=True)
        (semantic_dir / "model.yml").write_text(self.OSSIE_SAMPLE)
        provider = SemanticLayerProvider(semantic_dir, "demo")

        llm = RecordingLLM(["```sql\nSELECT SUM(amount) FROM loan;\n```"])
        sub = build_gen_sql_subgraph(make_services(llm))
        services = make_services(
            llm, connectors=demo_registry, kb=kb, semantic_layer=provider)
        node = graphs_module._make_gen_sql_node(services, sub)
        state = make_state(
            question="What is the total loans volume?",
            matched_tables=["loan"],
        )
        out = await node(state)
        assert out["sql"]
        prompt = " ".join(str(m.get("content", "")) for m in llm.calls[-1])
        # KB 的映射出现;实时层的 SUM(loan.amount) 作为 term 被去重
        assert "SUM(loan.amount) * 2" in prompt
        assert "total_loan_amount" in prompt


# ── gen_sql subgraph ─────────────────────────────────────


class TestFewShotRotation:
    def _state(self, shots=None):
        return GenSQLState(question="q", session_id="s1", run_id="r1", few_shots=shots)

    def test_rotates_leading_example_by_offset(self):
        from trove.workflow.graphs import _rotate_few_shots
        shots = [{"question": f"q{i}", "sql": f"sql{i}"} for i in range(3)]
        st = self._state(shots)
        r1 = _rotate_few_shots(st, 1)
        assert [s["question"] for s in r1.few_shots] == ["q1", "q2", "q0"]
        r2 = _rotate_few_shots(st, 2)
        assert [s["question"] for s in r2.few_shots] == ["q2", "q0", "q1"]
        # offset 超过长度取模循环(4 % 3 = 1)
        r4 = _rotate_few_shots(st, 4)
        assert [s["question"] for s in r4.few_shots] == ["q1", "q2", "q0"]
        # 原对象不被修改(纯函数)
        assert [s["question"] for s in st.few_shots] == ["q0", "q1", "q2"]

    def test_fewer_than_two_shots_returns_same_state(self):
        from trove.workflow.graphs import _rotate_few_shots
        single = self._state([{"question": "q0", "sql": "sql0"}])
        assert _rotate_few_shots(single, 1) is single
        empty = self._state([])
        assert _rotate_few_shots(empty, 1) is empty


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
        # Second and third calls used the fix prompt (默认 zh)
        assert "校验错误" in llm.calls[1][-1]["content"]
        assert "校验错误" in llm.calls[2][-1]["content"]

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
        llm = RecordingLLM(["query", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["verdict"] == "OK"
        assert final["retry_count"] == 0
        assert final["error"] == ""
        assert "SELECT name FROM students;" in final["sql"]
        assert final["row_count"] == 5
        assert "### 结果" in final["final_response"]
        assert len(llm.calls) == 3  # intent + gen_sql + reflect

    async def test_retry_loop_regenerates_with_reason(self, sqlite_registry, catalog):
        llm = RecordingLLM([
            "query", VALID_SQL, "RETRY: wrong grouping", "RETRY: wrong grouping",
            "TARGET: gen_sql", VALID_SQL, "OK",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["verdict"] == "OK"
        assert final["retry_count"] == 1
        assert final["error"] == ""
        assert len(llm.calls) == 7  # intent + gen + (reflect + rejudge) + judge + gen + reflect
        # The regenerated SQL prompt carried the reflect reason
        assert "wrong grouping" in llm.calls[5][-1]["content"]

    async def test_semantic_retry_cap_forces_accept(self, sqlite_registry, catalog):
        """两次一致判 RETRY 后,第二次达语义上限 → 强制接受,不再第 3 次
        重生成(修复前:语义拉锯可到 4 轮重生成)。"""
        llm = RecordingLLM([
            "query", VALID_SQL, "RETRY: a", "RETRY: a",
            "TARGET: gen_sql", VALID_SQL, "RETRY: b", "RETRY: b",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["retry_count"] == 1
        assert final["verdict"] == "OK"
        assert final["forced"] is True
        assert final["error"] == ""
        assert len(llm.calls) == 8  # intent + gen + (reflect+rejudge) + judge + gen + (reflect+rejudge)

    async def test_rejudge_disagreement_delivers(self, sqlite_registry, catalog):
        """A: 主裁决 RETRY 但 rejudge 判 OK(不一致)→ 结果直接交付,
        不经过 analyze_error 打回。"""
        llm = RecordingLLM([
            "query", VALID_SQL, "RETRY: gap semantics", "OK",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["verdict"] == "OK"
        assert final["forced"] is True
        assert final["retry_count"] == 0
        assert final["error"] == ""
        assert len(llm.calls) == 4  # intent + gen + reflect + rejudge(analyze_error 未执行)

    async def test_gen_sql_exhaustion_degrades_to_output(self, sqlite_registry, catalog):
        """gen_sql subgraph exhausts retries → execute/reflect skipped → error section."""
        llm = RecordingLLM(["query"] + [INVALID_SQL] * 3)
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert "3 attempts" in final["error"]
        assert final["row_count"] == -1  # execute_sql never ran
        assert "**错误**" in final["final_response"]
        assert len(llm.calls) == 4  # intent + 3 generate attempts; reflect never called

    async def test_execute_failure_degrades_to_output(self, sqlite_registry, catalog, monkeypatch):
        """执行失败 → 修正预算内重生成 → 耗尽后优雅降级（不再首错即降级）。"""
        monkeypatch.setattr(graphs_module, "MAX_REFLECT_RETRIES", 2)
        bad_sql = "```sql\nSELECT * FROM nonexistent;\n```"
        # 每轮修正：gen → analyze（诊断）→ gen…
        llm = RecordingLLM(["query", bad_sql, "diag", bad_sql, "diag", bad_sql])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"]
        assert final["retry_count"] == 2
        assert "**错误**" in final["final_response"]
        assert final["verdict"] == ""  # reflect skipped

    async def test_execute_error_feedback_fixes_sql(self, sqlite_registry, catalog):
        """执行错误反馈给 gen_sql → 修正后成功（修正闭环）。"""
        llm = RecordingLLM([
            "query",                                             # 意图
            "```sql\nSELECT * FROM nonexistent;\n```",           # 初稿（运行时错误）
            "diag: 表名错误",                                      # 错误诊断
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
        # 错误诊断 prompt 携带了执行错误信息
        assert "nonexistent" in llm.calls[2][-1]["content"]

    async def test_preexisting_error_passes_straight_to_output(self, sqlite_registry, catalog):
        graphs = build(make_services(RecordingLLM([]), catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state(error="upstream failed"))
        assert "upstream failed" in final["final_response"]
        assert final["row_count"] == -1

    async def test_error_analysis_reaches_regeneration(self, sqlite_registry, catalog):
        """analyze_error 的专家诊断必须注入重生成 prompt,而不是被丢弃。"""
        llm = RecordingLLM([
            "query",                                             # 意图
            "```sql\nSELECT * FROM nonexistent;\n```",           # 初稿(运行时错误)
            "diag: 表名错误,应使用 students",                      # 错误诊断
            "```sql\nSELECT name FROM students;\n```",           # 修正稿
            "OK",                                                # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["retry_count"] == 1
        # 重生成 prompt(call 3)携带专家诊断
        assert "diag: 表名错误" in llm.calls[3][-1]["content"]

    async def test_empty_result_reflect_short_circuits(self, sqlite_registry, catalog):
        """Zero rows → EMPTY verdict, no reflect LLM call."""
        llm = RecordingLLM(["query", "```sql\nSELECT name FROM students WHERE 0;\n```"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["verdict"] == "EMPTY"
        assert len(llm.calls) == 2  # intent + gen_sql


class TestIntentRouting:
    async def test_schema_intent_llm_answer(self, sqlite_registry, catalog):
        """「有哪些表」→ 强信号直路由，答案由 LLM 组织。"""
        llm = RecordingLLM(["metadata", "数据源共 1 张表：students（4 列）", "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state(question="有哪些表"))
        assert final["intent"] == "metadata"
        assert "students" in final["intent_answer"]
        assert final["sql"] == ""
        assert len(llm.calls) == 3  # 分类 + 答案 + 裁决

    async def test_lineage_intent_llm_answer(self, sqlite_registry, catalog):
        """血缘意图 → LLM 组织关联答案。"""
        adapter = await sqlite_registry.get()
        await adapter.execute(
            "CREATE TABLE district (district_id INTEGER PRIMARY KEY, name TEXT)"
        )
        await adapter.execute(
            "CREATE TABLE city (city_id INTEGER PRIMARY KEY, district_id INTEGER)"
        )
        llm = RecordingLLM(["metadata", "city 与 district 通过 city.district_id 关联"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="city 表的血缘")
        )
        assert final["intent"] == "metadata"
        assert "city.district_id" in final["intent_answer"]

    async def test_semantic_intent_llm_answer(self, sqlite_registry, catalog, tmp_path):
        """语义意图 → 术语材料进上下文，LLM 组织答案。"""
        from trove.services.kb.service import KbService

        from tests.helpers.kb import ossie_semantics_yaml

        kb = KbService(tmp_path / "proj")
        ds_dir = kb.kb_dir / sqlite_registry.default_name
        ds_dir.mkdir(parents=True)
        (ds_dir / "semantics.yml").write_text(ossie_semantics_yaml([
            {"term": "平均成绩", "mapping": "AVG(students.grade)",
             "tables": ["students"], "definition": "学生平均分"},
        ]))
        llm = RecordingLLM(["metadata", "平均成绩 → AVG(students.grade)，即学生平均分"])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=kb))
        final = await graphs["reflection"].ainvoke(
            make_state(question="平均成绩的定义")
        )
        assert final["intent"] == "metadata"
        assert "AVG(students.grade)" in final["intent_answer"]

    async def test_query_intent_still_runs_pipeline(self, sqlite_registry, catalog):
        """普通查询问题照常走生成流水线。"""
        llm = RecordingLLM(["query", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["intent"] == "query"
        assert final["row_count"] == 5

    async def test_schema_intent_answers_in_english(self, sqlite_registry, catalog):
        """配置 lang=en → 英文答案(不按问题语言检测)。"""
        llm = RecordingLLM(["metadata", "The datasource has 1 table: students (4 columns)"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state(question="list tables", lang="en"))
        assert final["intent"] == "metadata"
        assert "table" in final["intent_answer"].lower()
        assert "数据源" not in final["intent_answer"]

    async def test_answer_llm_failure_falls_back_to_template(self, sqlite_registry, catalog):
        """答案 LLM 失败 → 模板兜底（管线不中断）。"""
        class RaisingLLM:
            async def chat(self, model, messages, **kwargs):
                raise RuntimeError("down")

        graphs = build(make_services(RaisingLLM(), catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state(question="有哪些表"))
        assert final["intent"] == "metadata"
        assert "students" in final["intent_answer"]  # 模板清单


class TestMetadataFocus:
    async def _make(self, sqlite_registry):
        adapter = await sqlite_registry.get()
        await adapter.execute(
            "CREATE TABLE district (district_id INTEGER PRIMARY KEY, name TEXT)"
        )
        await adapter.execute(
            "CREATE TABLE loan (loan_id INTEGER PRIMARY KEY, account_id INTEGER)"
        )
        await adapter.execute(
            "CREATE TABLE order_t (order_id INTEGER PRIMARY KEY, account_id INTEGER)"
        )

    async def test_relation_only_question_llm_answer(self, sqlite_registry, catalog):
        """只问关联字段 → LLM 聚焦回答（用户案例）。"""
        await self._make(sqlite_registry)
        llm = RecordingLLM([
            "metadata",
            "account 通过 district_id 关联 district；\n"
            "loan 通过 account_id 关联 account；\n"
            "order 通过 account_id 关联 account。\n"
            "loan 与 order 之间无直接关联，均通过 account 间接关联。",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="district 与 account 通过什么字段关连？loan 和 order 呢？")
        )
        assert final["intent"] == "metadata"
        answer = final["intent_answer"]
        assert "district_id" in answer
        assert "account_id" in answer
        assert "数据源共" not in answer  # LLM 聚焦，不堆清单

    async def test_relation_variants(self, sqlite_registry, catalog):
        """变体问法（怎么连/相连）→ 弱信号 → LLM 确认 + 聚焦回答。"""
        await self._make(sqlite_registry)
        llm = RecordingLLM(["metadata", "account 与 loan 通过 account_id 相连"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="account 与 loan 怎么连接")
        )
        assert final["intent"] == "metadata"
        assert "account_id" in final["intent_answer"]

    async def test_indirect_relation_answer(self, sqlite_registry, catalog):
        """无直接关联的两表 → LLM 答案提示经共同表。"""
        await self._make(sqlite_registry)
        llm = RecordingLLM(["metadata", "loan 与 order 均通过 account 关联"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="loan 和 order 有什么关系")
        )
        assert "account" in final["intent_answer"]


class TestIntentLLMFallback:
    async def test_weak_signal_llm_classifies_metadata(self, sqlite_registry, catalog):
        """弱信号（裸「表」字）→ LLM 二分类确认 + 答案组织（2 次调用）。"""
        llm = RecordingLLM(["metadata", "students 表包含 id、name、grade、county 列", "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="students 表有什么")
        )
        assert final["intent"] == "metadata"
        assert "id" in final["intent_answer"]
        assert len(llm.calls) == 3  # 分类 + 答案 + 裁决

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
        """复合问题（含义 + 关系）→ LLM 逐件回答。"""
        adapter = await sqlite_registry.get()
        await adapter.execute(
            "CREATE TABLE courses (course_id INTEGER PRIMARY KEY, students_id INTEGER, title TEXT)"
        )
        llm = RecordingLLM([
            "metadata",
            "students 是学生表；courses 是课程表。\n"
            "两者通过 courses.students_id 与 students.id 关联。",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="students 和 courses 表分别什么含义？有什么关系")
        )
        assert final["intent"] == "metadata"
        assert "students" in final["intent_answer"]
        assert "courses" in final["intent_answer"]
        assert "students_id" in final["intent_answer"]


class TestGenerationTemperature:
    def test_temperature_rises_with_retries(self):
        from trove.workflow.graphs import generation_temperature
        assert generation_temperature(0) == 0.0
        assert generation_temperature(1) == 0.1
        assert generation_temperature(3) == 0.3
        assert generation_temperature(10) == 0.3  # 封顶


class AgenticLLM:
    """chat_full 脚本化：dict 响应（content/tool_calls）；chat 共享队列（reflect 单次裁决用）。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return self._responses.pop(0)

    async def chat(self, model, messages, **kwargs):
        self.calls.append(messages)
        return self._responses.pop(0)


class BrokenAgenticLLM(AgenticLLM):
    """chat_full 永远抛异常（loop 崩溃）；chat 走 classic 脚本化响应。"""

    def __init__(self, classic_responses):
        super().__init__([])
        self._classic = list(classic_responses)

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.calls.append(messages)
        raise RuntimeError("llm down")

    async def chat(self, model, messages, **kwargs):
        self.calls.append(messages)
        return self._classic.pop(0)


class TestAgenticNodes:
    async def test_loop_failure_falls_back_to_classic(self, sqlite_registry, catalog):
        """agentic 循环异常（如 LLM 500）→ 回退 classic 生成，不崩溃。

        回归：此前 result=None 时仍访问 result["tool_history"]，整图 TypeError。
        """
        llm = BrokenAgenticLLM(["query", VALID_SQL, "OK"])  # intent + classic 生成 + reflect 裁决
        graphs = build(make_services(llm, catalog, sqlite_registry), agentic=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["sql"] == "SELECT name FROM students;"
        assert final["row_count"] == 5

    async def test_gen_sql_tool_validation_round(self, sqlite_registry, catalog):
        """gen_sql ReAct：模型先调 validate_sql 工具自检，再交最终 SQL。"""
        llm = AgenticLLM([
            "query",  # 意图（chat）
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "validate_sql",
                 "arguments": '{"sql": "SELECT name FROM students"}'},
            ]},
            {"content": "```sql\nSELECT name FROM students;\n```", "tool_calls": []},
            {"content": "OK", "tool_calls": []},  # reflect（agentic 内容型）
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), agentic=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["sql"] == "SELECT name FROM students;"
        assert len(llm.calls) == 4  # 意图 + 工具轮 + 最终轮 + reflect
        assert final["row_count"] == 5

    async def test_gen_sql_probe_query_round(self, sqlite_registry, catalog):
        """gen_sql ReAct：模型先 probe_query 自证草稿(观测含 ok/row_count)，再定稿。"""
        llm = AgenticLLM([
            "query",  # 意图（chat）
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "probe_query",
                 "arguments": '{"sql": "SELECT name FROM students"}'},
            ]},
            {"content": "```sql\nSELECT name FROM students;\n```", "tool_calls": []},
            {"content": "OK", "tool_calls": []},  # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), agentic=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["sql"] == "SELECT name FROM students;"
        assert len(llm.calls) == 4  # 意图 + 探针轮 + 最终轮 + reflect
        # 观测进 tool 消息:ok true + 真实行数
        tool_msgs = [m for msgs in llm.calls for m in msgs if m.get("role") == "tool"]
        assert any('"ok": true' in m["content"] and "row_count" in m["content"] for m in tool_msgs)
        assert final["row_count"] == 5

    async def test_gen_sql_probe_harvests_sql_from_tool_history(self, sqlite_registry, catalog):
        """模型结尾 content 无 SQL 但 probe 过 → 从 tool_history 捞回 SQL。"""
        llm = AgenticLLM([
            "query",
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "probe_query",
                 "arguments": '{"sql": "SELECT name FROM students"}'},
            ]},
            {"content": "", "tool_calls": []},  # 结尾无 SQL 回显
            {"content": "OK", "tool_calls": []},  # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), agentic=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["sql"] == "SELECT name FROM students"  # 从工具参数捞回
        assert final["row_count"] == 5

    async def test_gen_sql_probe_write_rejected_safely(self, sqlite_registry, catalog):
        """probe 写语句 → ok:false 观测,管线正常完成,数据未被改动(只读性)。"""
        llm = AgenticLLM([
            "query",
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "probe_query",
                 "arguments": '{"sql": "DELETE FROM students"}'},
            ]},
            {"content": "```sql\nSELECT name FROM students;\n```", "tool_calls": []},
            {"content": "OK", "tool_calls": []},
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), agentic=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["sql"] == "SELECT name FROM students;"
        tool_msgs = [m for msgs in llm.calls for m in msgs if m.get("role") == "tool"]
        assert any('"ok": false' in m["content"] for m in tool_msgs)
        # 只读性验证:表仍在、行数不变
        r = await sqlite_registry.execute("SELECT COUNT(*) FROM students")
        assert r.rows[0][0] == 5

    async def test_gen_sql_check_result_round(self, sqlite_registry, catalog):
        """gen_sql ReAct：check_result 观测 OK → harness 自动定稿,省掉再调 finish 的一轮。"""
        llm = AgenticLLM([
            "query",  # 意图（chat）
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "check_result",
                 "arguments": '{"sql": "SELECT county, AVG(grade) FROM students GROUP BY county"}'},
            ]},
            "OK",  # reflect（loop 在 check 通过后即定稿,不再消耗下一轮）
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), agentic=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["sql"] == "SELECT county, AVG(grade) FROM students GROUP BY county"
        # 观测进轨迹:OK + 真实行数;无规则命中 → validation_hits 为空
        assert "OK (3 rows)" in final["reasoning_history"][0]["text"]
        assert final["validation_hits"] == []

    async def test_gen_sql_check_result_catches_violation(self, sqlite_registry, catalog):
        """count 题草稿按分组展开 → check_result 报 VIOLATION；模型改 SQL；命中记入 validation_hits。"""
        llm = AgenticLLM([
            "query",
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "check_result",
                 "arguments": '{"sql": "SELECT county, COUNT(*) FROM students GROUP BY county"}'},
            ]},
            {"content": "```sql\nSELECT COUNT(*) FROM students;\n```", "tool_calls": []},
            {"content": "OK", "tool_calls": []},  # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), agentic=True)
        final = await graphs["reflection"].ainvoke(
            make_state(question="How many students are there in total?"),
        )
        assert final["error"] == ""
        assert final["sql"] == "SELECT COUNT(*) FROM students;"
        # count 题结果单行单列,值为 5 名学生
        assert final["row_count"] == 1
        assert final["rows"][0][0] == 5
        # 违规命中随状态带出:count-multirow
        assert any(h["name"] == "count-multirow" for h in final["validation_hits"])

    async def test_gen_sql_check_result_write_rejected(self, sqlite_registry, catalog):
        """check_result 写语句 → ERROR 观测,管线正常完成,数据未被改动(只读性)。"""
        llm = AgenticLLM([
            "query",
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "check_result",
                 "arguments": '{"sql": "DELETE FROM students"}'},
            ]},
            {"content": "```sql\nSELECT name FROM students;\n```", "tool_calls": []},
            {"content": "OK", "tool_calls": []},
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), agentic=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["sql"] == "SELECT name FROM students;"
        tool_msgs = [m for msgs in llm.calls for m in msgs if m.get("role") == "tool"]
        assert any("write operations are not permitted" in m["content"] for m in tool_msgs)
        r = await sqlite_registry.execute("SELECT COUNT(*) FROM students")
        assert r.rows[0][0] == 5

    async def test_gen_sql_check_result_harvests_sql_from_tool_history(self, sqlite_registry, catalog):
        """模型结尾 content 无 SQL 但 check 过 → 从 tool_history 捞回 SQL。"""
        llm = AgenticLLM([
            "query",
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "check_result",
                 "arguments": '{"sql": "SELECT name FROM students"}'},
            ]},
            {"content": "", "tool_calls": []},  # 结尾无 SQL 回显
            {"content": "OK", "tool_calls": []},
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), agentic=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["sql"] == "SELECT name FROM students"  # 从工具参数捞回

    async def test_gen_sql_finish_tool_round(self, sqlite_registry, catalog):
        """模型用显式 finish(answer) 携带最终 SQL 定稿,harness 立即终止。"""
        llm = AgenticLLM([
            "query",  # 意图（chat）
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "finish",
                 "arguments": '{"answer": "```sql\\nSELECT name FROM students;\\n```"}'},
            ]},
            {"content": "OK", "tool_calls": []},  # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), agentic=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["sql"] == "SELECT name FROM students;"
        assert len(llm.calls) == 3  # 意图 + finish 轮 + reflect(无额外生成轮)

    async def test_gen_sql_guard_degrades_to_classic(self, sqlite_registry, catalog):
        """agent loop 一直调工具到护栏 → 降级到经典生成,而不是判失败。

        护栏降级链:agentic ReAct → (guard_hit) → 经典 generate/validate 子图。
        模型反复调 finish 但载荷无效 → 循环无法终止,触发护栏。
        """
        class GuardLLM:
            """chat_full 永远返回无效 finish(打转);chat 走经典脚本化响应。"""

            def __init__(self, classic_responses):
                self._classic = list(classic_responses)
                self.calls = []
                self.rounds = 0

            async def chat_full(self, model, messages, tools=None, **kwargs):
                self.calls.append(messages)
                self.rounds += 1
                return {"content": None, "tool_calls": [
                    {"id": f"c{self.rounds}", "name": "finish", "arguments": "{}"},
                ]}

            async def chat(self, model, messages, **kwargs):
                self.calls.append(messages)
                return self._classic.pop(0)

        llm = GuardLLM(["query", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry), agentic=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["sql"] == "SELECT name FROM students;"  # classic 兜底产出
        assert final["row_count"] == 5

    async def test_reflect_is_single_shot(self, sqlite_registry, catalog):
        """reflect 去工具化：即使有 connectors，裁决也只调一次 chat，绝不进 chat_full。"""
        class NoToolJudgeLLM:
            def __init__(self):
                self.chat_count = 0

            async def chat_full(self, model, messages, tools=None, **kwargs):
                if (kwargs.get("metadata") or {}).get("node") == "reflect":
                    raise AssertionError("reflect must not use chat_full/tools")
                return {"content": VALID_SQL, "tool_calls": []}  # gen 内容型单轮

            async def chat(self, model, messages, **kwargs):
                self.chat_count += 1
                return "OK"

        llm = NoToolJudgeLLM()
        graphs = build(make_services(llm, catalog, sqlite_registry), agentic=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["verdict"] == "OK"
        assert final["row_count"] == 5
        assert llm.chat_count == 2  # route_intent + reflect 各一次单次调用


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

    async def test_zero_match_refuses_without_fallback(self, sqlite_registry, catalog):
        """语义优先（Phase B,决策 4）：零命中 = 未覆盖 = 拒绝，不生成不执行。"""
        llm = RecordingLLM([])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="zzz 完全不相关的数据")
        )
        assert final["refusal"] is not None
        assert final["sql"] == ""
        assert final["row_count"] == -1

    async def test_matched_tables_proceed_normally(self, sqlite_registry, catalog):
        """有表匹配 → 正常走生成流程。"""
        llm = RecordingLLM(["query", "plan: use students", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry), planner=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["clarification_question"] == ""
        assert final["row_count"] == 5
        # 查询计划到达了 gen_sql 的生成 prompt
        assert "Query plan" in llm.calls[2][-1]["content"]


class TestRollbackRouting:
    """失败即判断：LLM 诊断根因并决定回退目标，带上下文重跑。"""

    async def test_execute_error_judge_routes_to_planner(self, sqlite_registry, catalog):
        """执行错误 → 判断回退 planner → 带修正上下文重定计划 → 重新生成成功。"""
        llm = RecordingLLM([
            "query",                                        # 意图
            "plan: 初版计划",                                # planner 首跑
            "```sql\nSELECT * FROM nonexistent;\n```",      # 初稿（执行失败）
            "类型: 计划偏差\nTARGET: planner",               # 判断：回退 planner
            "plan: 用 students 表按 county 分组",            # planner 重定计划
            "```sql\nSELECT name FROM students;\n```",      # 重新生成
            "OK",                                           # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), planner=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["row_count"] == 5
        assert final["rollback_target"] == "planner"
        assert len(llm.calls) == 7
        # planner 重跑时携带了修正上下文(默认中文配置 → 中文标签)
        planner_prompts = [
            str(m.get("content", ""))
            for call in llm.calls
            for m in call
            if "修正上下文" in str(m.get("content", ""))
        ]
        assert planner_prompts
        assert "no such table" in planner_prompts[0]

    async def test_judge_routes_to_schema_linking(self, sqlite_registry, catalog):
        """判断回退 schema_linking：重新选表后重新生成成功。"""
        llm = RecordingLLM([
            "query",                                        # 意图
            "```sql\nSELECT * FROM nonexistent;\n```",      # 初稿（执行失败）
            "判断: 漏了表\nTARGET: schema_linking",          # 判断：重做选表
            "```sql\nSELECT name FROM students;\n```",      # 重新生成
            "OK",                                           # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["row_count"] == 5
        assert final["rollback_target"] == "schema_linking"

    async def test_reflect_retry_goes_through_judge(self, sqlite_registry, catalog):
        """reflect RETRY 不再直回 gen_sql，而是先经 LLM 判断回退目标。"""
        llm = RecordingLLM([
            "query",                    # 意图
            VALID_SQL,                  # 初稿
            "RETRY: wrong grouping",    # reflect 裁决
            "RETRY: wrong grouping",    # rejudge 一致判 RETRY
            "TARGET: gen_sql",          # 判断：仍回 gen_sql
            VALID_SQL,                  # 重新生成
            "OK",                       # reflect 通过
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["verdict"] == "OK"
        assert final["retry_count"] == 1
        assert final["error"] == ""
        assert final["rollback_target"] == "gen_sql"
        assert len(llm.calls) == 7  # 意图 + gen + (reflect+rejudge) + judge + gen + reflect

    async def test_anti_loop_escalates_within_graph(self, sqlite_registry, catalog):
        """防打转：判断连续两次指回 gen_sql → 强制升级到 planner。"""
        llm = RecordingLLM([
            "query",                                        # 意图
            "plan: v1",                                     # planner 首跑
            "```sql\nSELECT * FROM nonexistent;\n```",      # gen pass1（执行失败）
            "TARGET: gen_sql",                              # judge pass1
            "```sql\nSELECT * FROM nonexistent;\n```",      # gen pass2（仍失败）
            "TARGET: gen_sql",                              # judge pass2 → 应被升级
            "plan: 用 students 表",                          # planner（升级后重跑）
            "```sql\nSELECT name FROM students;\n```",      # gen pass3 成功
            "OK",                                           # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), planner=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["row_count"] == 5
        # 升级生效：gen_sql 的重复判断被替换为 planner
        assert final["rollback_target"] == "planner"
        assert len(llm.calls) == 9  # 意图+planner+gen+judge+gen+judge+planner+gen+reflect

    async def test_execution_errors_not_mislabeled_regression(self, sqlite_registry, catalog):
        """连续不同的执行错误不误报 invalid(结果集签名对执行错误无意义)。"""
        llm = RecordingLLM([
            "query",                                        # 意图
            "```sql\nSELECT * FROM nonexistent_tbl;\n```",  # gen pass1 (exec error 1)
            "TARGET: gen_sql",                              # judge round1
            "```sql\nSELECT name FROM studnets;\n```",      # gen pass2 (exec error 2)
            "TARGET: gen_sql",                              # judge round2
            "```sql\nSELECT grade FROM studentss;\n```",    # gen pass3 (exec error 3)
            "TARGET: gen_sql",                              # judge round3
            "```sql\nSELECT name FROM students;\n```",      # gen pass4 OK
            "OK",                                           # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""  # 3 个不同执行错误不会触发 no-progress 提前降级
        # 新执行错误 ≠ improved:执行成功前都计无进展(计数 2 < 上限 3,
        # 第 4 轮执行成功,不触发降级)——但仍不误报 invalid
        assert final["last_progress"] == "none"
        assert final["no_progress_rounds"] == 2
        # judge prompt 不注入"同一错误重演"报告(不同执行错误 ≠ 无效修复)
        for msgs in llm.calls:
            text = " ".join(str(m.get("content", "")) for m in msgs)
            assert "same execution error as Round" not in text

    async def test_semantic_retry_does_not_escalate_across_failure_types(
        self, sqlite_registry, catalog,
    ):
        """防打转只对同一失败重演升档:语义 RETRY 不因上轮执行错误而误升级。"""
        llm = RecordingLLM([
            "query",                                        # 意图
            "plan: v1",                                     # planner 首跑
            "```sql\nSELECT * FROM nonexistent_tbl;\n```",  # gen pass1 (exec error)
            "TARGET: gen_sql",                              # judge round1 → last=gen_sql
            "```sql\nSELECT name FROM students;\n```",      # gen pass2 OK
            "RETRY: 语义不对，应该按 county 分组",           # reflect RETRY (纯语义)
            "RETRY: 语义不对，应该按 county 分组",           # rejudge 一致
            "TARGET: gen_sql",                              # judge round2: 不回退再升级
            "```sql\nSELECT county, COUNT(*) FROM students GROUP BY county;\n```",
            "OK",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), planner=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["verdict"] == "OK"
        assert final["rollback_target"] == "gen_sql"  # 未被升级到 planner
        # 意图+planner+gen+judge+gen+(reflect+rejudge)+judge+gen+reflect
        assert len(llm.calls) == 10

    async def test_kb_exact_match_regenerates_on_correction_rounds(
        self, sqlite_registry, catalog,
    ):
        """KB 精确命中的 SQL 执行失败后,修正轮重新生成而非重发同一 SQL。"""

        class FakeKB:
            """KB stub:与问题逐词一致的示例(指向不存在的表 → 执行必失败)。"""

            async def ensure_synced(self, default_datasource=None):
                pass

            async def search_examples(self, q, ds, limit=5, tables=None,
                                      all_tables=None, per_table=False):
                return [SimpleNamespace(
                    question="Average grade by county",
                    sql="SELECT grade FROM nonexistent_kb", tags=[], template=None,
                )]

            async def list_rules(self, ds):
                return []

            async def search_lessons(self, q, ds, limit=3, tables=None, all_tables=None):
                return []

            async def search_terms(self, q, ds, tables=None, all_tables=None):
                return []

            async def table_notes(self, tables, ds):
                return {}

        llm = RecordingLLM([
            "query",                                        # 意图
            "TARGET: gen_sql",                              # judge round1
            "```sql\nSELECT name FROM students;\n```",      # gen pass2(修正轮)
            "OK",                                           # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=FakeKB()))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        # 修正轮走了真实生成,而非重发 KB 示例 SQL(否则 execute 必再次失败)
        assert " ".join(final["sql"].split()).rstrip(";") == "SELECT name FROM students"
        assert final["retry_count"] == 1


class TestFixedGraph:
    async def test_no_reflection(self, sqlite_registry, catalog):
        llm = RecordingLLM(["query", VALID_SQL])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["fixed"].ainvoke(make_state())
        assert final["verdict"] == ""  # reflect never ran
        assert final["row_count"] == 5
        assert "### 结果" in final["final_response"]
        assert len(llm.calls) == 2  # intent + gen_sql

    async def test_execute_error_feedback_retries(self, sqlite_registry, catalog):
        """fixed 图同样带执行错误修正闭环。"""
        llm = RecordingLLM([
            "query",
            "```sql\nSELECT * FROM nonexistent;\n```",
            "```sql\nSELECT name FROM students;\n```",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["fixed"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["row_count"] == 5
        assert len(llm.calls) == 3  # intent + 初稿 + 修正稿


class TestEmptyGraph:
    async def test_pass_through(self):
        graphs = build(make_services(RecordingLLM([])))
        final = await graphs["empty"].ainvoke(make_state())
        assert "(未执行任何查询)" in final["final_response"]


class TestConclusionWiring:
    """conclusion 节点接线:结论 LLM 生成并置于回答开头(结论前置)。"""

    async def test_reflection_graph_emits_conclusion(self, sqlite_registry, catalog):
        config = AgentConfig(target="mock/model", insights=True, conclusion=True)
        llm = RecordingLLM([
            "query",            # intent
            VALID_SQL,          # gen_sql
            "OK",               # reflect
            "洞察一条",           # insights
            "结论:共 5 名学生",    # conclusion
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry, config=config))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["conclusion"] == "结论:共 5 名学生"
        assert final["insights"] == ["洞察一条"]
        assert "### 结论" in final["final_response"]
        # 结论位于生成的 SQL 之前
        assert final["final_response"].index("### 结论") < final["final_response"].index("### 生成的 SQL")
        assert len(llm.calls) == 5  # intent + gen + reflect + insights + conclusion

    async def test_disabled_config_skips_conclusion(self, sqlite_registry, catalog):
        llm = RecordingLLM(["query", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["conclusion"] == ""
        assert "### 结论" not in final["final_response"]
        assert len(llm.calls) == 3  # 未新增结论 LLM 调用


class TestParseDateWiring:
    """parse_date 节点接线:确定性解析,不消耗 LLM 调用,结果注入生成 prompt。"""

    async def test_resolves_and_injects_into_gen_prompt(self, sqlite_registry, catalog):
        llm = RecordingLLM(["query", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="students 最近7天的平均成绩是多少")
        )
        import re

        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} ~ \d{4}-\d{2}-\d{2}", final["time_context"])
        # 时间块注入 gen_sql 的用户提示词(不新增 LLM 调用)
        assert "Resolved time range" in llm.calls[1][-1]["content"]
        assert len(llm.calls) == 3  # intent + gen_sql + reflect

    async def test_fixed_graph_also_resolves(self, sqlite_registry, catalog):
        llm = RecordingLLM(["query", VALID_SQL])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["fixed"].ainvoke(
            make_state(question="students 最近7天的平均成绩是多少")
        )
        assert final["time_context"]
        assert "Resolved time range" in llm.calls[1][-1]["content"]
        assert len(llm.calls) == 2  # intent + gen_sql

    async def test_no_time_expression_passes_through(self, sqlite_registry, catalog):
        llm = RecordingLLM(["query", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["time_context"] == ""
        assert "Resolved time range" not in llm.calls[1][-1]["content"]

    async def test_disabled_config_skips_parsing(self, sqlite_registry, catalog):
        llm = RecordingLLM(["query", VALID_SQL, "OK"])
        config = AgentConfig(target="mock/model", date_parser=False)
        graphs = build(make_services(llm, catalog, sqlite_registry, config=config))
        final = await graphs["reflection"].ainvoke(
            make_state(question="students 最近7天的平均成绩是多少")
        )
        assert final["time_context"] == ""
        assert "Resolved time range" not in llm.calls[1][-1]["content"]

    async def test_empty_graph_unaffected(self):
        llm = RecordingLLM([])
        graphs = build(make_services(llm))
        final = await graphs["empty"].ainvoke(
            make_state(question="students 最近7天的平均成绩是多少")
        )
        assert final["time_context"] == ""
        assert len(llm.calls) == 0


class TestKnowledgeBaseGraph:
    def _kb(self, tmp_path, datasource):
        from trove.services.kb.service import KbService

        from tests.helpers.kb import ossie_semantics_yaml

        kb = KbService(tmp_path / "proj")
        ds_dir = kb.kb_dir / datasource
        ds_dir.mkdir(parents=True)
        (ds_dir / "semantics.yml").write_text(ossie_semantics_yaml([
            {"term": "平均成绩", "mapping": "AVG(students.grade)",
             "tables": ["students"], "definition": "学生平均分"},
        ]))
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
            "query",
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
        assert "知识库" in final["final_response"]
        # The reference example reached the LLM prompt
        assert any(
            "Reference examples" in " ".join(
                str(m.get("content", "")) for m in call
            )
            for call in llm.calls
        )

    async def test_no_kb_has_no_kb_hits(self, sqlite_registry, catalog):
        """kb 未配置时状态与行为不变（回归）。"""
        llm = RecordingLLM(["query", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["kb_hits"] == []
        assert "知识库" not in final["final_response"]

    async def test_few_shots_rotated_across_alt_candidates(
        self, sqlite_registry, catalog, tmp_path,
    ):
        """P2-7:备选候选按索引轮换 few-shot 首条目(示例锚点各异防趋同)。

        主候选拿到相关度排序的完整示例集;第 i 个备选整体左移 i 位,
        不同候选锚定不同的参考示例——投票分歧不再被共享范例抹平。
        """
        from trove.services.kb.service import KbService
        kb = KbService(tmp_path / "proj")
        ds_dir = kb.kb_dir / sqlite_registry.default_name
        ds_dir.mkdir(parents=True)
        (ds_dir / "schema_notes.yml").write_text(
            "tables:\n  - name: students\n    description: 学生表\n"
            "    columns:\n      - name: grade\n        description: 成绩\n",
            encoding="utf-8",
        )
        (ds_dir / "examples.yml").write_text(
            """
examples:
  - question: 各地区的平均成绩是多少
    sql: SELECT county, AVG(grade) FROM students GROUP BY county
    tags: [成绩]
  - question: 各县的最高成绩是多少
    sql: SELECT county, MAX(grade) FROM students GROUP BY county
    tags: [成绩]
  - question: 各城市有多少学生成绩
    sql: SELECT county, COUNT(*) FROM students GROUP BY county
    tags: [成绩]
""",
            encoding="utf-8",
        )
        ex_qs = ["各地区的平均成绩是多少", "各县的最高成绩是多少", "各城市有多少学生成绩"]
        sql = "SELECT county, AVG(grade) FROM students GROUP BY county"
        llm = RecordingLLM([
            "query",
            f"```sql\n{sql};\n```",  # 主候选
            f"```sql\n{sql};\n```",  # 备1-4(同结果,投票一致)
            f"```sql\n{sql};\n```",
            f"```sql\n{sql};\n```",
            f"```sql\n{sql};\n```",
            "OK",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=kb),
                       multi_candidate=True)
        final = await graphs["reflection"].ainvoke(
            make_state(question="students 各县市的成绩情况汇总")
        )
        assert final["error"] == ""

        def example_order(prompt):
            """按 prompt 中出现位置排序示例问题(提取真实出场顺序)。"""
            return sorted(
                (q for q in ex_qs if q in prompt), key=lambda q: prompt.find(q),
            )

        primary = " ".join(str(m.get("content", "")) for m in llm.calls[1])
        order0 = example_order(primary)
        assert len(order0) == 3  # 三个示例都进了主候选 prompt
        n = len(order0)
        for i, call in enumerate(llm.calls[2:6], start=1):
            prompt = " ".join(str(m.get("content", "")) for m in call)
            off = i % n  # offset = 候选索引,超长度取模循环
            assert example_order(prompt) == order0[off:] + order0[:off], (
                f"备选 {i} 应轮换 {off} 位"
            )

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
        llm = RecordingLLM(["query", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=kb))
        await graphs["reflection"].ainvoke(make_state())
        assert "Data source rules" in llm.calls[1][-1]["content"]

    async def test_context_budget_drops_low_priority_blocks(self, sqlite_registry, catalog, monkeypatch):
        """预算不足时低优先级块（history）被排除，核心 schema 保留。

        history 已拆成逐轮条目:预算 2 连单轮(~5 tokens)都塞不下 →
        整块排除;核心 schema(非可选块)始终保留。
        """
        monkeypatch.setattr(
            graphs_module, "COMPLEXITY_BUDGET_TOKENS",
            {"simple": 2, "standard": 2, "complex": 2},
        )
        llm = RecordingLLM(["query", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(history="user: 平均成绩是多少\nassistant: 85 分")
        )
        assert final["error"] == ""
        gen_prompt = llm.calls[1][-1]["content"]
        assert "Conversation history" not in gen_prompt  # 低优先级被预算排除
        assert "Database schema" in gen_prompt                 # 核心保留
        usage = final["context_usage"]
        assert any(u["name"] == "history" and not u["included"] for u in usage)

    async def test_complexity_tier_drives_budget(self, sqlite_registry, catalog, monkeypatch):
        """复杂度档位驱动预算:simple 档��算小(history 被裁),complex 档预算大(history 保留)。"""
        monkeypatch.setattr(
            graphs_module, "COMPLEXITY_BUDGET_TOKENS",
            {"simple": 2, "standard": 2500, "complex": 2500},
        )
        history = "user: 平均成绩是多少\nassistant: 85 分"

        monkeypatch.setattr(graphs_module, "grade_complexity", lambda *a, **k: "simple")
        llm = RecordingLLM(["query", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        await graphs["reflection"].ainvoke(make_state(history=history))
        assert "Conversation history" not in llm.calls[1][-1]["content"]

        monkeypatch.setattr(graphs_module, "grade_complexity", lambda *a, **k: "complex")
        llm2 = RecordingLLM(["query", VALID_SQL, "OK"])
        graphs2 = build(make_services(llm2, catalog, sqlite_registry))
        await graphs2["reflection"].ainvoke(make_state(history=history))
        assert "Conversation history" in llm2.calls[1][-1]["content"]

    async def test_few_shots_trimmed_by_score_in_tight_budget(
        self, sqlite_registry, catalog, monkeypatch,
    ):
        """预算吃紧时按分数保留最相关示例(高分进 prompt,低分被裁)。"""

        class ScoredKB:
            async def ensure_synced(self, default_datasource=None):
                pass

            async def search_examples(self, q, ds, limit=5, tables=None,
                                      all_tables=None, per_table=False):
                return [
                    SimpleNamespace(
                        question="high q", tags=[], template=False, score=9,
                        sql="SELECT 1 FROM students WHERE grade = 90",
                    ),
                    SimpleNamespace(
                        question="mid q", tags=[], template=False, score=5,
                        sql="SELECT 2 FROM students WHERE grade = 80",
                    ),
                    SimpleNamespace(
                        question="low q", tags=[], template=False, score=1,
                        sql="SELECT 3 FROM students WHERE grade = 70",
                    ),
                ]

            async def list_rules(self, ds):
                return []

            async def search_lessons(self, q, ds, limit=3, tables=None, all_tables=None):
                return []

            async def search_terms(self, q, ds, tables=None, all_tables=None):
                return []

            async def table_notes(self, tables, ds):
                return {}

        # 每条示例约 13 tokens;standard 档预算 20 → 只装得下最高分那条
        monkeypatch.setattr(
            graphs_module, "COMPLEXITY_BUDGET_TOKENS",
            {"simple": 2500, "standard": 20, "complex": 2500},
        )
        llm = RecordingLLM(["query", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=ScoredKB()))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        prompt = llm.calls[1][-1]["content"]
        assert "grade = 90" in prompt          # 高分示例保留
        assert "grade = 70" not in prompt      # 低分示例被预算裁掉
        usage = {u["name"]: u for u in final["context_usage"]}
        assert usage["few_shots"]["items_total"] == 3
        assert usage["few_shots"]["items_included"] == 1
        assert final["cache_prefix_tokens"] > 0

    async def test_history_reaches_generation_prompt(self, sqlite_registry, catalog):
        """会话历史注入 gen_sql 的生成 prompt。"""
        llm = RecordingLLM(["query", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        await graphs["reflection"].ainvoke(
            make_state(history="user: 平均成绩是多少\nassistant: 85 分")
        )
        assert "平均成绩是多少" in llm.calls[1][-1]["content"]

    async def test_invalid_fix_detected_and_versions_injected(self, sqlite_registry, catalog):
        """版本链+定点修复: pass2 结果与 pass1 相同(无效修复) → 回归报告进诊断;
        pass3 生成 prompt 注入完整版本链。"""
        llm = RecordingLLM([
            "query",                                                       # 意图
            "```sql\nSELECT name FROM students;\n```",                   # p1 主（5行,规则失败）
            "TARGET: gen_sql",                                           # 诊断1（记录 v1）
            "```sql\nSELECT name FROM students ORDER BY name;\n```",     # p2 主（同 5 行 → 无效修复）
            "TARGET: gen_sql",                                           # 诊断2（应含 Invalid fix）
            "```sql\nSELECT COUNT(*) FROM students;\n```",               # p3 主（单值 → 通过）
            "OK",                                                        # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="how many students are there")
        )
        assert final["error"] == ""
        assert final["row_count"] == 1
        assert final["retry_count"] == 2
        # 回归硬检查: 诊断2 的 prompt 含无效修复报告(对比 Round 1)
        diag2 = " ".join(m["content"] for m in llm.calls[4])
        assert "Invalid fix" in diag2
        assert "Round 1" in diag2
        # 版本链注入: pass3 生成 prompt 含两个失败版本
        gen3 = " ".join(m["content"] for m in llm.calls[5])
        assert "Failed SQL versions" in gen3
        assert "Round 1" in gen3 and "Round 2" in gen3

    async def test_validation_rule_fixes_count_question(self, sqlite_registry, catalog):
        """count 问题先返回多行 → 规则失败 → 带理由重新生成 → 修正成功。"""
        llm = RecordingLLM([
            "query",                                         # 意图
            "```sql\nSELECT name FROM students;\n```",   # 多行（规则失败）
            "diag: count 应单值",                          # 错误诊断
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
        assert "校验规则" in llm.calls[3][-1]["content"]

    async def test_validation_rule_fixes_empty_list_question(self, sqlite_registry, catalog):
        """list 问题返回 0 行 → 规则触发重新生成 → 非空结果。"""
        llm = RecordingLLM([
            "query",
            "```sql\nSELECT name FROM students WHERE 0;\n```",
            "diag: 空结果可疑",
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
        """5 候选(1 主 + 4 备)结果全部一致 → 高置信放行（无需修正）。"""
        llm = RecordingLLM([
            "query",                                                       # 意图
            VALID_SQL,                                                   # 主候选
            "```sql\nSELECT name FROM students ORDER BY name;\n```",     # 备1（同结果）
            "```sql\nSELECT name FROM students ORDER BY name DESC;\n```",  # 备2（同结果）
            "```sql\nSELECT name FROM students ORDER BY name ASC;\n```",  # 备3（同结果）
            "```sql\nSELECT name FROM students WHERE name IS NOT NULL;\n```",  # 备4（同结果）
            "OK",                                                        # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), multi_candidate=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["retry_count"] == 0
        assert final["row_count"] == 5
        assert len(final["candidates"]) == 4
        assert len(llm.calls) == 7  # 意图 + 主 + 4 备 + reflect

    async def test_candidate_disagreement_regenerates(self, sqlite_registry, catalog):
        """5 候选投票平局 → 反馈重生成 → 一致后放行。"""
        llm = RecordingLLM([
            "query",                                                       # 意图
            VALID_SQL,                                                   # pass1 主（5 行）
            "```sql\nSELECT name FROM students WHERE 0;\n```",           # 备1（0 行）
            "```sql\nSELECT name FROM students LIMIT 1;\n```",           # 备2（1 行）
            "```sql\nSELECT name FROM students LIMIT 2;\n```",           # 备3（2 行）
            "```sql\nSELECT name FROM students LIMIT 3;\n```",           # 备4（3 行）
            "TARGET: gen_sql",                                           # pass1 诊断
            VALID_SQL,                                                   # pass2 主
            "```sql\nSELECT name FROM students ORDER BY name;\n```",     # 备1（同结果）
            "```sql\nSELECT name FROM students ORDER BY name DESC;\n```",  # 备2（同结果）
            "```sql\nSELECT name FROM students ORDER BY name ASC;\n```",  # 备3（同结果）
            "```sql\nSELECT name FROM students WHERE name IS NOT NULL;\n```",  # 备4（同结果）
            "OK",                                                        # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), multi_candidate=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["retry_count"] == 1
        assert final["row_count"] == 5
        # 一致性失败理由进入了错误诊断 prompt
        assert "候选 SQL 结果不一致" in llm.calls[6][-1]["content"]
        # 任务1: 第 2 轮生成的 prompt 注入第 1 轮失败假设黑名单(诊断后跨轮累积)
        prompt2 = " ".join(m["content"] for m in llm.calls[7])
        assert "Rejected hypotheses" in prompt2
        assert "SELECT name FROM students" in prompt2  # 上一轮失败 SQL 摘要
        # 任务2: Fixer 模式 — 注入上一版 SQL 全文,指示局部修复
        assert "Previous SQL" in prompt2
        assert "SELECT name FROM students;" in prompt2

    async def test_candidates_accumulate_across_retries(self, sqlite_registry, catalog):
        """任务3: 候选池跨轮累积 — 平局打回后旧候选保留,新候选加入,重试=加票。

        三轮:pass1 4 备选结果互异(平局) → pass2 又 4 个新结果(仍平局,
        旧候选与新候选同组加票) → pass3 主+4 备同结果形成 5 票多数放行。
        最终候选池 = 旧 8 + 新 4 = 12(去重后),而非每轮覆盖成 4。
        """
        llm = RecordingLLM([
            "query",                                                       # 意图
            VALID_SQL,                                                   # p1 主（5行）
            "```sql\nSELECT name FROM students WHERE 0;\n```",           # p1 备1（0行）
            "```sql\nSELECT name FROM students LIMIT 1;\n```",           # p1 备2（1行）
            "```sql\nSELECT name FROM students LIMIT 2;\n```",           # p1 备3（2行）
            "```sql\nSELECT name FROM students LIMIT 3;\n```",           # p1 备4（3行）
            "TARGET: gen_sql",                                           # p1 诊断
            "```sql\nSELECT name FROM students ORDER BY name;\n```",     # p2 主（5行）
            "```sql\nSELECT name FROM students WHERE 1=0;\n```",         # p2 备1（0行→与旧备1同组）
            "```sql\nSELECT name FROM students LIMIT 1 OFFSET 0;\n```",  # p2 备2（1行→与旧备2同组）
            "```sql\nSELECT name FROM students LIMIT 2 OFFSET 0;\n```",  # p2 备3（2行→与旧备3同组）
            "```sql\nSELECT name FROM students LIMIT 3 OFFSET 0;\n```",  # p2 备4（3行→与旧备4同组）
            "TARGET: gen_sql",                                           # p2 诊断
            "```sql\nSELECT name FROM students ORDER BY name ASC;\n```",  # p3 主（5行）
            "```sql\nSELECT name FROM students ORDER BY name DESC;\n```",  # p3 备1（5行）
            "```sql\nSELECT name FROM students ORDER BY name;\n```",      # p3 备2（5行）
            "```sql\nSELECT name FROM students WHERE name IS NOT NULL;\n```",  # p3 备3（5行）
            "```sql\nSELECT name FROM students ORDER BY name COLLATE NOCASE;\n```",  # p3 备4（5行）
            "OK",                                                        # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), multi_candidate=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["row_count"] == 5
        assert final["retry_count"] == 2
        # 候选池跨轮累积:pass1 4 + pass2 4 + pass3 4(文本均不同) = 12
        assert len(final["candidates"]) == 12
        # 旧候选(平局轮的解释)仍留在池中参与投票
        assert any("WHERE 0" in c for c in final["candidates"])
        assert any("LIMIT 3" in c for c in final["candidates"])

    async def test_alt_candidates_generate_in_parallel(self, sqlite_registry, catalog):
        """任务4: 备选子图并行生成 — 并发活跃 LLM 调用数 > 1(串行恒为 1)。"""
        class ConcurrencyLLM:
            def __init__(self, responses):
                self._responses = list(responses)
                self.calls = []
                self.active = 0
                self.max_active = 0

            async def chat(self, model, messages, **kwargs):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.calls.append(messages)
                await asyncio.sleep(0.005)  # 放大交错窗口
                resp = self._responses.pop(0)
                self.active -= 1
                return resp

            async def chat_full(self, model, messages, tools=None, **kwargs):
                self.calls.append(messages)
                return {"content": self._responses.pop(0), "tool_calls": []}

        llm = ConcurrencyLLM([
            "query",                                                       # 意图
            VALID_SQL,                                                   # 主候选
            "```sql\nSELECT name FROM students ORDER BY name;\n```",     # 备1
            "```sql\nSELECT name FROM students ORDER BY name DESC;\n```",  # 备2
            "```sql\nSELECT name FROM students ORDER BY name ASC;\n```",  # 备3
            "```sql\nSELECT name FROM students WHERE name IS NOT NULL;\n```",  # 备4
            "OK",                                                        # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), multi_candidate=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["row_count"] == 5
        assert len(final["candidates"]) == 4
        assert llm.max_active >= 2  # 备选并发生成(串行时恒为 1)


class TestNoSQLExit:
    """「这不是 SQL 问题」的结构性出口：意图验证 + reflect/analyze_error NO_SQL。"""

    async def _make_disp(self, sqlite_registry):
        adapter = await sqlite_registry.get()
        await adapter.execute(
            "CREATE TABLE disp (disp_id INTEGER PRIMARY KEY, "
            "client_id INTEGER, account_id INTEGER, type TEXT)"
        )

    async def test_llm_intent_routes_definitional_question_to_metadata(self, sqlite_registry, catalog):
        """「disp 表是啥」→ LLM 裁决 metadata → 验证（已知表命中）→ 直答，零 SQL。"""
        await self._make_disp(sqlite_registry)
        llm = RecordingLLM([
            "metadata",
            "students 是学生成绩表，记录学生姓名、成绩与所在县。",
            "OK",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="students 表是啥")
        )
        assert final["intent"] == "metadata"
        assert "students" in final["intent_answer"]
        assert final["sql"] == ""
        assert len(llm.calls) == 3  # 意图 + 答案 + 裁决

    async def test_reflect_no_sql_exits_to_metadata_without_looping(self, sqlite_registry, catalog):
        """query 管线走到 reflect，裁决 NO_SQL → answer_metadata，不进入重试循环。"""
        await self._make_disp(sqlite_registry)
        llm = RecordingLLM([
            "query",
            VALID_SQL,
            "NO_SQL: 这不是数据查询，是表含义问题",
            "students 是学生成绩表。",
            "OK",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="students 表是啥")
        )
        assert final["verdict"] == "NO_SQL"
        assert final["no_sql"] is True
        assert final["retry_count"] == 0  # 未消耗修正预算
        assert "students" in final["intent_answer"]
        assert "SELECT" not in final["final_response"]
        assert len(llm.calls) == 5  # 意图 + gen + 裁决 + 答案 + 答案裁决

    async def test_analyze_error_no_sql_exits_via_consensus_disagreement(self, sqlite_registry, catalog):
        """候选不一致 → 诊断判定 NO_SQL → answer_metadata（清掉陈旧反馈）。"""
        await self._make_disp(sqlite_registry)
        llm = RecordingLLM([
            "query",                                                # 意图
            VALID_SQL,                                             # 主候选（5 行）
            "```sql\nSELECT name FROM students WHERE 0;\n```",     # 备1（0 行）
            "```sql\nSELECT name FROM students LIMIT 1;\n```",     # 备2（1 行）
            "```sql\nSELECT name FROM students LIMIT 2;\n```",     # 备3（2 行）
            "```sql\nSELECT name FROM students LIMIT 3;\n```",     # 备4（3 行）
            "NO_SQL: 定义问题不是数据查询",                           # 诊断
            "students 是学生成绩表。",                            # 元数据答案
            "OK",                                                  # 答案裁决
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), multi_candidate=True)
        final = await graphs["reflection"].ainvoke(
            make_state(question="students 表是啥")
        )
        assert final["no_sql"] is True
        assert final["error_feedback"] == ""
        assert final["retry_count"] == 1  # select 的不一致反馈计了一轮
        assert "students" in final["intent_answer"]
        assert len(llm.calls) == 9

    async def test_query_verdict_overridden_by_strong_regex(self, sqlite_registry, catalog):
        """LLM 误判 query，但强 metadata 信号 + 无数据信号 → 验证覆写为 metadata。"""
        llm = RecordingLLM([
            "query",
            "客户数指去重后的客户数量。",
            "OK",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="客户数的定义")
        )
        assert final["intent"] == "metadata"
        assert "客户" in final["intent_answer"]
        assert final["sql"] == ""


class TestRouteIntentObservability:
    """route_intent 观测:节点返回完整 LLM 详情 + 意图证据,供日志/诊断。"""

    async def test_uses_fast_model_when_configured(self):
        """意图分类是判别任务:配置 model_fast 时走 fast 档,不烧推理模型。"""
        from trove.workflow.graphs import make_route_intent

        class IntentLLM:
            def __init__(self):
                self.model = None

            async def chat(self, model, messages, **kwargs):
                self.model = model
                return "query"

        llm = IntentLLM()
        node = make_route_intent(
            llm=llm,
            config=AgentConfig(target="mock/model", model_fast="fast/model"),
            catalog=None, kb=None, connectors=None,
        )
        await node(make_state(question="What is the average loan amount?"))
        assert llm.model == "fast/model"

    async def test_returns_llm_detail_and_intent_evidence(self):
        from trove.workflow.graphs import make_route_intent

        class IntentLLM:
            async def chat(self, model, messages, **kwargs):
                return "metadata"

        node = make_route_intent(
            llm=IntentLLM(), config=AgentConfig(target="mock/model"),
            catalog=None, kb=None, connectors=None,
        )
        # "表结构"命中强信号,且不带 count/list 等数据信号
        delta = await node(make_state(question="loan 的表结构是怎样的?"))

        assert delta["intent"] == "metadata"
        assert delta["llm"]["model"] == "mock/model"
        assert delta["llm"]["elapsed_ms"] >= 0
        assert delta["llm"]["input_preview"]      # 意图分类 system prompt
        assert delta["llm"]["output_preview"] == "metadata"
        assert delta["intent_evidence"] == {
            "strong_match": True,
            "data_signal": False,
            "write_signal": False,
            "chitchat_signal": False,
            "correction_signal": False,
            "followup_signal": False,
            "history_present": False,
            "weak_signal": True,
            "llm_verdict": "metadata",
            "llm_error": "",
            "mentioned_table": False,
            "term_hit": False,
            "rewritten": False,
            "substituted": False,
        }

    async def test_llm_failure_recorded_as_evidence(self):
        """LLM 不可用时:回退正则路由,但证据里记下 llm_error。"""
        from trove.workflow.graphs import make_route_intent

        class BoomLLM:
            async def chat(self, model, messages, **kwargs):
                raise RuntimeError("api down")

        node = make_route_intent(
            llm=BoomLLM(), config=AgentConfig(target="mock/model"),
            catalog=None, kb=None, connectors=None,
        )
        delta = await node(make_state(question="平均成绩是多少"))

        assert delta["intent"] == "query"  # 正则兜底默认 query
        assert delta["llm"] is None
        assert delta["intent_evidence"]["llm_verdict"] is None
        assert "api down" in delta["intent_evidence"]["llm_error"]


class TestRouteIntentNewIntents:
    """五意图路由:write / chitchat / correction(纯反馈重跑 + 省略式追问重写)。"""

    async def test_write_routes_directly(self):
        from trove.workflow.graphs import make_route_intent

        node = make_route_intent(llm=None, config=AgentConfig(),
                                 catalog=None, kb=None, connectors=None)
        delta = await node(make_state(question="删除loan表的记录"))
        assert delta["intent"] == "write"

    async def test_chitchat_routes_directly(self):
        from trove.workflow.graphs import make_route_intent

        node = make_route_intent(llm=None, config=AgentConfig(),
                                 catalog=None, kb=None, connectors=None)
        delta = await node(make_state(question="你好"))
        assert delta["intent"] == "chitchat"

    async def test_pure_feedback_substitutes_previous_question(self):
        """「不对」→ 重跑上一问:question 替换为历史里的上一问,intent 按它重判。"""
        from trove.workflow.graphs import make_route_intent

        class IntentLLM:
            async def chat(self, model, messages, **kwargs):
                return "correction"

        node = make_route_intent(llm=IntentLLM(), config=AgentConfig(target="mock/model"),
                                 catalog=None, kb=None, connectors=None)
        delta = await node(make_state(
            question="不对",
            history="user: 哪个地区的平均贷款金额最高?\nassistant: 北京\n",
        ))
        assert delta["question"] == "哪个地区的平均贷款金额最高?"
        assert delta["intent"] == "query"
        assert delta["rewritten_question"] == "不对"
        assert delta["intent_evidence"]["substituted"] is True

    async def test_feedback_without_history_keeps_correction(self):
        """无历史可重跑 → 保留 correction,由 answer_correction 给引导话术。"""
        from trove.workflow.graphs import make_route_intent

        node = make_route_intent(llm=None, config=AgentConfig(),
                                 catalog=None, kb=None, connectors=None)
        delta = await node(make_state(question="不对"))
        assert delta["intent"] == "correction"
        assert "question" not in delta  # 未替换

    async def test_elliptical_followup_rewrites_then_reclassifies(self):
        """「那北京呢」→ 一次 LLM 重写补全 → 再走一次完整证据链分类。"""
        from trove.workflow.graphs import make_route_intent

        calls: list[list[dict]] = []

        class RewriteLLM:
            async def chat(self, model, messages, **kwargs):
                calls.append(messages)
                # 第1次=初始分类,第2次=重写,第3次=重写后问题分类
                return ["query", "北京的平均贷款金额是多少", "query"][
                    min(len(calls) - 1, 2)
                ]

        node = make_route_intent(llm=RewriteLLM(), config=AgentConfig(target="mock/model"),
                                 catalog=None, kb=None, connectors=None)
        delta = await node(make_state(
            question="那北京呢",
            history="user: 哪个地区平均贷款最高?\nassistant: 北京\n",
        ))
        assert delta["question"] == "北京的平均贷款金额是多少"
        assert delta["rewritten_question"] == "那北京呢"
        assert delta["intent"] == "query"
        assert delta["intent_evidence"]["rewritten"] is True
        assert len(calls) == 3  # 分类 + 重写 + 重分类
        # 重写调用带上了对话历史
        assert "哪个地区平均贷款最高" in calls[1][0]["content"]

    async def test_followup_rewrite_failure_degrades_to_guidance(self):
        """重写失败(LLM 异常)→ 保留 correction 走引导话术,不瞎路由。"""
        from trove.workflow.graphs import make_route_intent

        class BoomLLM:
            async def chat(self, model, messages, **kwargs):
                raise RuntimeError("api down")

        node = make_route_intent(llm=BoomLLM(), config=AgentConfig(target="mock/model"),
                                 catalog=None, kb=None, connectors=None)
        delta = await node(make_state(
            question="那北京呢",
            history="user: 哪个地区平均贷款最高?\nassistant: 北京\n",
        ))
        assert delta["intent"] == "correction"
        assert "question" not in delta  # 未替换问题
        assert delta["intent_evidence"]["rewritten"] is False

    def test_route_after_intent_returns_intent_value(self):
        """_route_after_intent 返回 intent 值,节点映射在条件边表里。"""
        from trove.workflow.graphs import _route_after_intent

        assert _route_after_intent(make_state(intent="write")) == "write"
        assert _route_after_intent(make_state(intent="chitchat")) == "chitchat"
        assert _route_after_intent(make_state(intent="correction")) == "correction"
        assert _route_after_intent(make_state(intent="metadata")) == "metadata"
        assert _route_after_intent(make_state(intent="query")) == "query"

    async def test_write_refused_e2e(self, sqlite_registry, catalog):
        """写意图走完整图 → 拒绝话术,不生成 SQL。"""
        llm = RecordingLLM(["write"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(
            make_state(question="删除loan表的记录")
        )
        assert final["intent"] == "write"
        assert "只读" in final["final_response"]
        assert final["sql"] == ""

    async def test_chitchat_answered_without_sql_e2e(self, sqlite_registry, catalog):
        llm = RecordingLLM(["chitchat"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state(question="你好"))
        assert final["intent"] == "chitchat"
        assert "Trove" in final["final_response"]
        assert final["sql"] == ""

    async def test_correction_without_history_guidance_e2e(self, sqlite_registry, catalog):
        """无历史可重跑的纯反馈 → answer_correction 引导话术。"""
        llm = RecordingLLM(["correction"])
        graphs = build(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state(question="不对"))
        assert final["intent"] == "correction"
        assert "重算" in final["final_response"]
        assert final["sql"] == ""

    async def test_terminal_nodes_answer_without_llm(self):
        from trove.workflow.nodes.terminal import (
            answer_chitchat,
            answer_correction,
            answer_reject,
        )

        reject = await answer_reject(make_state(lang="zh"))
        assert "只读" in reject["intent_answer"]
        assert reject["intent_answer"]

        greet = await answer_chitchat(make_state(question="你好"))
        thanks = await answer_chitchat(make_state(question="谢谢"))
        assert greet["intent_answer"]
        assert greet["intent_answer"] != thanks["intent_answer"]

        guidance = await answer_correction(make_state(lang="zh"))
        assert "重算" in guidance["intent_answer"]


class TestFixModeWiring:
    """缺口3: fix_mode 从 analyze_error 判定到重生成 prompt 的全链路。"""

    async def test_execution_error_routes_to_fixer_mode(self, sqlite_registry, catalog):
        """执行错误 → judge 判定 fixer → 重生成 prompt 显式要求实现级修复。"""
        llm = RecordingLLM([
            "query",                                        # 意图
            "plan: v1",                                     # planner
            "```sql\nSELECT * FROM nonexistent;\n```",      # gen pass1（执行失败）
            "TARGET: gen_sql",                              # judge → fixer
            "```sql\nSELECT name FROM students;\n```",      # gen pass2 成功
            "OK",                                           # reflect
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), planner=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"] == ""
        assert final["fix_mode"] == "fixer"          # 判定结果落 state
        assert final["last_progress"] == "first"     # 仅一轮 judge（首轮无基线）
        # pass2 的 prompt 显式含 Fix mode 指令（实现级修复）
        assert "Fix mode: implementation-level repair" in llm.calls[4][-1]["content"]

    async def test_no_progress_degrades_before_ladder_exhausts(self, sqlite_registry, catalog):
        """缺口5: 连续 3 轮无效修复(结果签名不变) → judge 提前止损,不再打回。

        每轮换回退目标规避 ladder 升级干扰——证明止损来自 no_progress
        计数而非防打转护栏。
        """
        bad = "```sql\nSELECT * FROM nonexistent;\n```"
        llm = RecordingLLM([
            "query",
            "plan: v1",                         # planner
            bad,                                # gen pass1 → 执行失败
            "TARGET: gen_sql",                  # judge1: first, 计数 0
            bad,                                # gen pass2（复制旧错误）
            "TARGET: planner",                  # judge2: invalid, 计数 1
            "plan: v2",                         # planner 重跑(回退目标)
            bad,                                # gen pass3
            "TARGET: schema_linking",           # judge3: invalid, 计数 2
            "query",                            # schema_linking 重跑(LLM 判定)
            bad,                                # gen pass4
            "TARGET: gen_sql",                  # judge4: invalid, 计数 3 → 降级
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry), planner=True)
        final = await graphs["reflection"].ainvoke(make_state())
        assert "无进展" in final["error"]        # 优雅降级,不再打回
        assert final["no_progress_rounds"] == 3
        assert final["last_progress"] == "invalid"
        assert "**错误**" in final["final_response"]


# ── 自适应减负:确定性快径 + 复杂度分级开关 ───────────────


class _FastPathKB:
    """带确定性模板 + schema_notes 的 KB:快径命中与复杂度 simple 判据共用。"""

    def __init__(self, tmp_path, datasource):
        from trove.services.kb.service import KbService

        self.kb = KbService(tmp_path / "proj")
        ds_dir = self.kb.kb_dir / datasource
        ds_dir.mkdir(parents=True)
        (ds_dir / "schema_notes.yml").write_text(
            """
tables:
  - name: students
    description: student records
    columns:
      - name: grade
        description: test score
""",
            encoding="utf-8",
        )
        # 术语:让 schema_linking 产出 term 命中 → kb_hits 非空,
        # grade_complexity 的 simple 判据才满足(语义证据是必要条件)
        from tests.helpers.kb import ossie_semantics_yaml

        (ds_dir / "semantics.yml").write_text(ossie_semantics_yaml([
            {"term": "students", "aliases": ["student"], "mapping": "students.id",
             "tables": ["students"], "definition": "student record"},
        ]))
        (ds_dir / "examples.yml").write_text(
            """
examples:
  - question: How many records are in the students table?
    sql: SELECT COUNT(*) FROM students
    tags: [students, count, aggregation]
    template: true
  - question: What is the average grade?
    sql: SELECT AVG(grade) FROM students
    tags: [students, grade, aggregation]
    template: true
    aggregate: true
""",
            encoding="utf-8",
        )


class TestFastPathGraph:
    async def test_template_hit_skips_llm_path(self, sqlite_registry, catalog, tmp_path):
        """模板命中:SQL 直接执行,planner/gen_sql/reflect 的 LLM 全部不调用。"""
        kb = _FastPathKB(tmp_path, sqlite_registry.default_name).kb
        llm = RecordingLLM(["query"])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=kb))
        final = await graphs["reflection"].ainvoke(
            make_state(question="How many students are there?")
        )
        assert final["fast_path"] is True
        assert final["sql"] == "SELECT COUNT(*) FROM students"
        assert final["verdict"] == "OK"
        assert final["row_count"] == 1
        assert len(llm.calls) == 1  # 仅 intent——planner/gen_sql/reflect 全未调用
        kinds = {h.get("kind") for h in final["kb_hits"]}
        assert "template" in kinds

    async def test_miss_uses_full_pipeline(self, sqlite_registry, catalog, tmp_path):
        """模板未命中(无聚合词/表不锚)→ 正常链路,快径零痕迹。"""
        kb = _FastPathKB(tmp_path, sqlite_registry.default_name).kb
        llm = RecordingLLM(["query", VALID_SQL, "OK"])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=kb))
        final = await graphs["reflection"].ainvoke(
            make_state(question="Who is in the students table?")
        )
        assert final["fast_path"] is False
        assert len(llm.calls) == 3  # intent + gen_sql + reflect

    async def test_reflect_skips_for_fast_path_sql(self, sqlite_registry, catalog, tmp_path):
        """快径 SQL 即使走到 reflect 也不调用 LLM 裁决(kb_exact 同理由)。"""
        kb = _FastPathKB(tmp_path, sqlite_registry.default_name).kb
        llm = RecordingLLM(["query"])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=kb))
        final = await graphs["reflection"].ainvoke(
            make_state(question="How many students are there?")
        )
        assert final["verdict"] == "OK"
        assert final["reason"] == "fast path deterministic template match (kb init)"
        assert len(llm.calls) == 1

    async def test_fast_path_disabled_by_config(self, sqlite_registry, catalog, tmp_path):
        """fast_path 配置关闭 → 快径不启用,走正常链路。"""
        kb = _FastPathKB(tmp_path, sqlite_registry.default_name).kb
        cfg = AgentConfig(target="mock/model", fast_path=False)
        llm = RecordingLLM([
            "query",
            "```sql\nSELECT COUNT(*) FROM students;\n```",
            "OK",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=kb, config=cfg))
        final = await graphs["reflection"].ainvoke(
            make_state(question="How many students are there?")
        )
        assert final["fast_path"] is False
        assert len(llm.calls) == 3


class TestComplexitySwitch:
    async def test_simple_plan_skips_multi_candidate(self, sqlite_registry, catalog, tmp_path):
        """planner 产出 simple plan → 复杂度 simple:agentic 降级为经典子图、
        跳过 4 个备选温度子图(无候选池)。"""
        kb = _FastPathKB(tmp_path, sqlite_registry.default_name).kb
        plan = '{"tables": ["students"], "aggregation": "COUNT", "answer_columns": ["count(*)"]}'
        llm = RecordingLLM([
            "query",
            plan,
            "```sql\nSELECT COUNT(*) FROM students;\n```",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=kb),
                       multi_candidate=True, planner=True, agentic=True)
        final = await graphs["reflection"].ainvoke(make_state(
            question="How many students have a grade?",
            kb_hits=[{"kind": "term", "term": "students", "mapping": "students.id",
                      "definition": "student record", "tables": ["students"]}],
        ))
        assert final["complexity"] == "simple"
        assert final["candidates"] == []  # 跳过多候选
        assert len(llm.calls) == 3  # intent + planner + gen_sql(经典);无 reflect

    async def test_complex_plan_keeps_multi_candidate(self, sqlite_registry, catalog, tmp_path):
        """≥3 聚合 → complex:候选池照常生成。"""
        kb = _FastPathKB(tmp_path, sqlite_registry.default_name).kb
        # 聚合 ≥ 3 即 complex(validate_plan 不校验 aggregation 文本,表不虚构)
        plan = ('{"tables": ["students"], "aggregation": ["COUNT(*)", "AVG(grade)", "SUM(grade)"], '
                '"answer_columns": ["count(*)"]}')
        llm = RecordingLLM([
            "query",
            plan,
            "```sql\nSELECT COUNT(*) FROM students;\n```",
            # 备选温度子图:同结果组(全部单行单列),避免 select 平局打回
            "```sql\nSELECT COUNT(*) FROM students WHERE 1=1;\n```",
            "```sql\nSELECT COUNT(id) FROM students;\n```",
            "```sql\nSELECT COUNT(grade) FROM students;\n```",
            "```sql\nSELECT COUNT(*) FROM students;\n```",
            "OK",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=kb),
                       multi_candidate=True, planner=True, agentic=True)
        final = await graphs["reflection"].ainvoke(make_state(
            question="How many students have a grade?",
            kb_hits=[{"kind": "term", "term": "students", "mapping": "students.id",
                      "definition": "student record", "tables": ["students"]}],
        ))
        assert final["complexity"] == "complex"
        assert len(final["candidates"]) >= 1


class TestReflectSkipGraph:
    def _simple_kb(self, tmp_path, datasource):
        return _FastPathKB(tmp_path, datasource).kb

    async def test_rules_pass_simple_skips_judge(self, sqlite_registry, catalog, tmp_path):
        """validate 规则全过 + 复杂度 simple → reflect 不调用 LLM 裁决。"""
        kb = self._simple_kb(tmp_path, sqlite_registry.default_name)
        plan = '{"tables": ["students"], "aggregation": "COUNT", "answer_columns": ["count(*)"]}'
        llm = RecordingLLM([
            "query",
            plan,
            "```sql\nSELECT COUNT(*) FROM students;\n```",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=kb),
                       multi_candidate=True, planner=True, agentic=True)
        final = await graphs["reflection"].ainvoke(make_state(
            question="How many students have a grade?",
            kb_hits=[{"kind": "term", "term": "students", "mapping": "students.id",
                      "definition": "student record", "tables": ["students"]}],
        ))
        assert final["verdict"] == "OK"
        assert final["rules_passed"] is True
        assert len(llm.calls) == 3  # intent + planner + gen_sql;无 reflect
        assert final["reason"] == "deterministic rules passed; reflect skipped"

    async def test_reflect_skip_off_keeps_judge(self, sqlite_registry, catalog, tmp_path):
        """reflect_skip=off → 规则全过也照常裁决。"""
        kb = self._simple_kb(tmp_path, sqlite_registry.default_name)
        cfg = AgentConfig(target="mock/model", reflect_skip="off")
        plan = '{"tables": ["students"], "aggregation": "COUNT", "answer_columns": ["count(*)"]}'
        llm = RecordingLLM([
            "query",
            plan,
            "```sql\nSELECT COUNT(*) FROM students;\n```",
            "OK",
        ])
        graphs = build(make_services(llm, catalog, sqlite_registry, kb=kb, config=cfg),
                       multi_candidate=True, planner=True, agentic=True)
        final = await graphs["reflection"].ainvoke(make_state(
            question="How many students have a grade?",
            kb_hits=[{"kind": "term", "term": "students", "mapping": "students.id",
                      "definition": "student record", "tables": ["students"]}],
        ))
        assert final["verdict"] == "OK"
        assert len(llm.calls) == 4  # intent + planner + gen_sql + reflect
