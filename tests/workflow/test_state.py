"""Graph state schema tests."""

import pytest
from pydantic import ValidationError

from trove.workflow.state import WorkflowState, GenSQLState


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
        assert state.candidates == []
        assert state.clarification_question == ""
        assert state.plan == ""
        assert state.consensus is True

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

    def test_attempts_are_ints(self):
        state = GenSQLState(question="q", schema_context="", dialect="sqlite", attempts=2)
        assert state.attempts == 2
