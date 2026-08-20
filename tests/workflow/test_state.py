"""Graph state schema tests."""

import pytest
from pydantic import ValidationError

from trove.workflow.state import (
    WorkflowState,
    GenSQLState,
    MAX_REASONING_ENTRIES,
    MAX_REJECTED_HYPOTHESES,
    MAX_SQL_VERSIONS,
    _cap_reasoning_history,
    _cap_rejected_hypotheses,
    _cap_sql_versions,
    budget_exhausted,
)


def test_budget_exhausted_boundary():
    assert not budget_exhausted(0, 10)
    assert not budget_exhausted(9, 10)
    assert budget_exhausted(10, 10)  # 达到上限即耗尽
    assert budget_exhausted(11, 10)
    assert budget_exhausted(0, 0)  # 上限 0 = 一开始就耗尽


class TestWorkflowState:
    def test_defaults_for_run_artifacts(self):
        """A fresh state has empty artifacts before nodes run."""
        state = WorkflowState(session_id="s1", question="average grade?")
        assert state.sql == ""
        assert state.row_count == -1
        assert state.verdict == ""
        assert state.retry_count == 0
        assert state.error == ""
        assert state.error_feedback == ""
        assert state.final_response == ""
        assert state.kb_hits == []
        assert state.history == ""
        assert state.time_context == ""
        assert state.candidates == []
        assert state.clarification_question == ""
        assert state.plan == ""
        assert state.consensus is True
        assert state.intent == "query"
        assert state.intent_answer == ""
        assert state.no_sql is False

    def test_session_id_and_question_required(self):
        with pytest.raises(ValidationError):
            WorkflowState()  # missing both
        with pytest.raises(ValidationError):
            WorkflowState(question="q")  # missing session_id

    def test_round_trips_through_graph_serialization(self):
        """State must survive JSON round-trip (checkpointer serializes it)."""
        state = WorkflowState(
            session_id="s1",
            question="q",
            sql="SELECT 1",
            rows=[[1], ["x"]],
            row_count=2,
            verdict="OK",
            retry_count=1,
        )
        restored = WorkflowState.model_validate_json(state.model_dump_json())
        assert restored == state


class TestGenSQLState:
    def test_defaults(self):
        state = GenSQLState(question="q", schema_context="", dialect="sqlite")
        assert state.sql == ""
        assert state.attempts == 0
        assert state.validation_errors == []
        assert state.error == ""
        assert state.error_feedback == ""
        assert state.time_context == ""

    def test_attempts_are_ints(self):
        state = GenSQLState(question="q", schema_context="", dialect="sqlite", attempts=2)
        assert state.attempts == 2


class TestGenSQLStateFromWorkflow:
    def _wf(self, **overrides) -> WorkflowState:
        base = dict(
            session_id="s1",
            question="q",
            run_id="r1",
            lang="zh",
            history="user: prev",
            evidence="hint",
            time_context="2024-01-01 ~ 2024-01-31",
            plan="Query plan (follow it...)",
            sql="SELECT 1",
            reason="wrong grouping",
            error_feedback="no such table",
            error_analysis="misread intent",
            rejected_hypotheses=[{"sql": "SELECT 1", "reason": "bad"}],
            sql_versions=[{"sql": "SELECT 1", "sig": "s", "round": 1}],
            fix_mode="revisor",
        )
        base.update(overrides)
        return WorkflowState(**base)

    def test_copies_context_fields(self):
        state = self._wf()
        sub = GenSQLState.from_workflow(state, dialect="mysql")
        assert sub.question == "q"
        assert sub.session_id == "s1"
        assert sub.run_id == "r1"
        assert sub.schema_context == state.schema_context
        assert sub.dialect == "mysql"
        assert sub.lang == "zh"
        assert sub.time_context == state.time_context
        assert sub.evidence == state.evidence
        assert sub.error_feedback == "no such table"
        assert sub.error_analysis == "misread intent"
        assert sub.rejected_hypotheses == state.rejected_hypotheses
        assert sub.sql_versions == state.sql_versions
        assert sub.fix_mode == "revisor"

    def test_derives_reflect_reason_and_previous_sql_on_correction(self):
        state = self._wf()
        sub = GenSQLState.from_workflow(state, dialect="sqlite")
        assert sub.reflect_reason == "wrong grouping"
        assert sub.previous_sql == "SELECT 1"  # 修正轮注入上一版失败 SQL

    def test_previous_sql_empty_on_first_pass(self):
        state = self._wf(error_feedback="", error_analysis="", reason="")
        sub = GenSQLState.from_workflow(state, dialect="sqlite")
        assert sub.previous_sql == ""

    def test_included_gates_budget_blocks(self):
        state = self._wf()
        sub = GenSQLState.from_workflow(
            state, dialect="sqlite", included={"plan"},
            few_shots=[{"question": "q", "sql": "SELECT 1"}],
            term_notes=[{"term": "t"}],
            lessons=[{"pattern": "p"}],
            rules=["r"],
        )
        assert sub.plan == state.plan       # 预算内 → 注入
        assert sub.history == ""            # 预算外 → 清空
        assert sub.few_shots is None
        assert sub.term_notes is None
        assert sub.lessons is None
        assert sub.rules is None

    def test_included_none_injects_all_blocks(self):
        state = self._wf()
        sub = GenSQLState.from_workflow(
            state, dialect="sqlite",
            few_shots=[{"question": "q", "sql": "SELECT 1"}],
            rules=["r"],
        )
        assert sub.history == state.history
        assert sub.plan == state.plan
        assert sub.few_shots == [{"question": "q", "sql": "SELECT 1"}]
        assert sub.rules == ["r"]

    def test_reasoning_context_passed_through(self):
        state = self._wf()
        sub = GenSQLState.from_workflow(state, dialect="sqlite", reasoning_context="[gen_sql] trail")
        assert sub.reasoning_context == "[gen_sql] trail"

    def test_history_override_wins_over_state(self):
        """item 级预算裁剪后的历史轮优先于 state.history。"""
        state = self._wf(history="user: full history\nassistant: old answer")
        sub = GenSQLState.from_workflow(
            state, dialect="sqlite", included={"history"}, history="user: kept turn",
        )
        assert sub.history == "user: kept turn"

    def test_schema_context_override_wins_over_state(self):
        """预算裁剪后的 schema 优先于 state.schema_context。"""
        state = self._wf()
        sub = GenSQLState.from_workflow(
            state, dialect="sqlite", schema_context="Table: kept\nColumns: a",
        )
        assert sub.schema_context == "Table: kept\nColumns: a"


class TestCappedReducers:
    """修正轮累积产物写入即裁剪:版本链/黑名单/思考痕迹不会无限增长。"""

    def test_sql_versions_capped(self):
        existing = [{"sql": f"SELECT {i}", "round": i} for i in range(1, MAX_SQL_VERSIONS + 3)]
        out = _cap_sql_versions(existing, [{"sql": "SELECT 99", "round": 99}])
        assert len(out) == MAX_SQL_VERSIONS
        assert out[-1]["round"] == 99  # 最新版保留
        assert [v["round"] for v in out] == [4, 5, 99]  # 只留最近 N 版

    def test_sql_versions_regression_needs_latest_only(self):
        # 回归判定只看上一版(sql_versions[-1]);裁旧版不影响
        out = _cap_sql_versions([], [{"sql": "SELECT 1", "round": 1}])
        assert out == [{"sql": "SELECT 1", "round": 1}]

    def test_rejected_hypotheses_capped(self):
        existing = [{"sql": f"S{i}", "reason": "r"} for i in range(MAX_REJECTED_HYPOTHESES + 5)]
        out = _cap_rejected_hypotheses(existing, [{"sql": "new", "reason": "r"}])
        assert len(out) == MAX_REJECTED_HYPOTHESES
        assert out[-1]["sql"] == "new"

    def test_reasoning_history_capped(self):
        existing = [{"node": "gen_sql", "text": f"t{i}"} for i in range(MAX_REASONING_ENTRIES + 4)]
        out = _cap_reasoning_history(existing, [{"node": "gen_sql", "text": "new"}])
        assert len(out) == MAX_REASONING_ENTRIES
        assert out[-1]["text"] == "new"

    def test_append_when_under_limit(self):
        out = _cap_sql_versions([{"sql": "S1"}], [{"sql": "S2"}])
        assert out == [{"sql": "S1"}, {"sql": "S2"}]

    def test_ignore_none_update(self):
        assert _cap_sql_versions([{"sql": "S1"}], None) == [{"sql": "S1"}]
