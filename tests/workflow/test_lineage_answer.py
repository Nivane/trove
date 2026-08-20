"""Metadata-answer lineage integration (deterministic, zero-LLM fallback path)."""

from __future__ import annotations

from trove.services.lineage.service import LineageService
from trove.workflow.nodes.answer import make_answer_metadata
from trove.workflow.state import WorkflowState


class TestLineageAnswer:
    async def test_fallback_renders_column_lineage(self, sqlite_registry, catalog, tmp_path):
        svc = LineageService(tmp_path)
        await svc.ingest_definition(
            "CREATE VIEW grade_stats AS SELECT county, avg(grade) AS avg_grade "
            "FROM students GROUP BY county",
            "test_db", dialect="sqlite",
        )
        node = make_answer_metadata(
            catalog=catalog, connectors=sqlite_registry, lineage=svc,
        )
        state = WorkflowState(
            session_id="s1", question="grade_stats.avg_grade 是怎么算出来的",
        )
        out = await node(state)
        answer = out["intent_answer"]
        assert "grade_stats" in answer
        assert "avg_grade" in answer
        assert "students" in answer  # 生产源表

    async def test_fallback_renders_table_level_lineage(self, sqlite_registry, catalog, tmp_path):
        svc = LineageService(tmp_path)
        await svc.ingest_definition(
            "CREATE VIEW grade_stats AS SELECT county, avg(grade) AS avg_grade "
            "FROM students GROUP BY county",
            "test_db", dialect="sqlite",
        )
        await svc.record_query(
            "SELECT avg_grade FROM grade_stats", "test_db", dialect="sqlite",
        )
        node = make_answer_metadata(
            catalog=catalog, connectors=sqlite_registry, lineage=svc,
        )
        state = WorkflowState(session_id="s1", question="grade_stats 的数据来源和下游消费")
        out = await node(state)
        answer = out["intent_answer"]
        assert "grade_stats" in answer
        assert "students" in answer
        assert "avg_grade" in answer  # 下游消费列可见

    async def test_no_records_renders_empty_notice(self, sqlite_registry, catalog, tmp_path):
        svc = LineageService(tmp_path)
        node = make_answer_metadata(
            catalog=catalog, connectors=sqlite_registry, lineage=svc,
        )
        state = WorkflowState(session_id="s1", question="students 表的上游是什么")
        out = await node(state)
        assert out["intent_answer"]

    async def test_no_lineage_service_is_noop(self, sqlite_registry, catalog):
        node = make_answer_metadata(catalog=catalog, connectors=sqlite_registry)
        state = WorkflowState(session_id="s1", question="students 表的数据来源")
        out = await node(state)
        answer = out["intent_answer"]
        # 无 lineage 服务 → 落回通用关联回答(不会抛错)
        assert "students" in answer