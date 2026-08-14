"""LangGraph topology tests: subgraph retry loop, reflect loop, degradation."""

import pytest

from trove.core.config import AgentConfig
from trove.workflow.state import WorkflowState, GenSQLState
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
        graphs = build_graphs(make_services(llm, catalog, sqlite_registry))
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
        graphs = build_graphs(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["verdict"] == "OK"
        assert final["retry_count"] == 1
        assert final["error"] == ""
        # The regenerated SQL prompt carried the reflect reason
        assert "wrong grouping" in llm.calls[2][-1]["content"]

    async def test_retry_cap_forces_accept(self, sqlite_registry, catalog):
        llm = RecordingLLM([
            VALID_SQL, "RETRY: a", VALID_SQL, "RETRY: b", VALID_SQL, "RETRY: c",
        ])
        graphs = build_graphs(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["retry_count"] == 2
        assert final["verdict"] == "OK"
        assert final["error"] == ""
        assert len(llm.calls) == 6  # 3 generate passes + 3 reflect calls

    async def test_gen_sql_exhaustion_degrades_to_output(self, sqlite_registry, catalog):
        """gen_sql subgraph exhausts retries → execute/reflect skipped → error section."""
        llm = RecordingLLM([INVALID_SQL] * 3)
        graphs = build_graphs(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert "3 attempts" in final["error"]
        assert final["row_count"] == -1  # execute_sql never ran
        assert "**Error**" in final["final_response"]
        assert len(llm.calls) == 3  # reflect never called

    async def test_execute_failure_degrades_to_output(self, sqlite_registry, catalog):
        llm = RecordingLLM(["```sql\nSELECT * FROM nonexistent;\n```"])
        graphs = build_graphs(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["error"]
        assert "**Error**" in final["final_response"]
        assert final["verdict"] == ""  # reflect skipped

    async def test_preexisting_error_passes_straight_to_output(self, sqlite_registry, catalog):
        graphs = build_graphs(make_services(RecordingLLM([]), catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state(error="upstream failed"))
        assert "upstream failed" in final["final_response"]
        assert final["row_count"] == -1

    async def test_empty_result_reflect_short_circuits(self, sqlite_registry, catalog):
        """Zero rows → EMPTY verdict, no reflect LLM call."""
        llm = RecordingLLM(["```sql\nSELECT name FROM students WHERE 0;\n```"])
        graphs = build_graphs(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["verdict"] == "EMPTY"
        assert len(llm.calls) == 1  # only gen_sql


class TestFixedGraph:
    async def test_no_reflection(self, sqlite_registry, catalog):
        llm = RecordingLLM([VALID_SQL])
        graphs = build_graphs(make_services(llm, catalog, sqlite_registry))
        final = await graphs["fixed"].ainvoke(make_state())
        assert final["verdict"] == ""  # reflect never ran
        assert final["row_count"] == 5
        assert "## Answer" in final["final_response"]
        assert len(llm.calls) == 1


class TestEmptyGraph:
    async def test_pass_through(self):
        graphs = build_graphs(make_services(RecordingLLM([])))
        final = await graphs["empty"].ainvoke(make_state())
        assert "(No query executed)" in final["final_response"]


class TestKnowledgeBaseGraph:
    def _kb(self, tmp_path):
        from trove.services.kb.service import KbService

        kb = KbService(tmp_path / "proj")
        kb.kb_dir.mkdir(parents=True)
        (kb.kb_dir / "semantics.yml").write_text(
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
        (kb.kb_dir / "schema_notes.yml").write_text(
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
        (kb.kb_dir / "examples.yml").write_text(
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
        kb = self._kb(tmp_path)
        llm = RecordingLLM([
            "```sql\nSELECT county, AVG(grade) FROM students GROUP BY county;\n```",
            "OK",
        ])
        graphs = build_graphs(make_services(llm, catalog, sqlite_registry, kb=kb))
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
        graphs = build_graphs(make_services(llm, catalog, sqlite_registry))
        final = await graphs["reflection"].ainvoke(make_state())
        assert final["kb_hits"] == []
        assert "Knowledge base" not in final["final_response"]
