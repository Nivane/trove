"""Graph state schemas for Trove workflows.

Two state classes:
  - WorkflowState: main-graph state, carried through a full query run
  - GenSQLState: gen_sql subgraph state for the internal validate-retry loop

Conversation message history is NOT part of graph state (dual-track
persistence): session messages live in SessionStore, graph state lives
in the LangGraph checkpointer.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any

from pydantic import BaseModel, Field


class WorkflowState(BaseModel):
    """State carried through one workflow (graph) execution."""

    session_id: str
    question: str

    # Compact conversation history (prior exchanges) for follow-up questions
    history: str = ""

    # Knowledge base hits (term matches + example matches).
    # operator.add: updates from different nodes accumulate.
    kb_hits: Annotated[list[dict[str, Any]], operator.add] = Field(default_factory=list)

    # schema_linking artifacts
    matched_tables: list[str] = Field(default_factory=list)
    schema_context: str = ""

    # gen_sql artifacts
    sql: str = ""
    dialect: str = "sqlite"

    # execute_sql artifacts
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = -1  # -1 = not executed
    execution_time_ms: float = 0.0

    # reflect artifacts + retry loop counter
    verdict: str = ""  # OK / RETRY / EMPTY
    reason: str = ""
    retry_count: int = 0  # reflect → gen_sql loop count (cap 2)
    forced: bool = False  # reflect accepted a RETRY at the retry cap

    # graceful degradation channel: first node failure message wins
    error: str = ""

    # execution-error feedback: execute_sql failures route back to gen_sql
    # with this message (shared retry budget); cleared on success
    error_feedback: str = ""

    # output artifact
    final_response: str = ""


class GenSQLState(BaseModel):
    """State for the gen_sql subgraph (generate → validate retry loop)."""

    question: str
    schema_context: str = ""
    dialect: str = "sqlite"
    reflect_reason: str = ""  # reason from a previous reflect RETRY (empty on first pass)

    sql: str = ""
    attempts: int = 0
    validation_errors: list[str] = Field(default_factory=list)
    error: str = ""

    # execution-error feedback from a previous pass (injected into the prompt)
    error_feedback: str = ""

    # conversation history for follow-up questions
    history: str = ""

    # Knowledge base material for prompt injection
    few_shots: list[dict[str, Any]] = Field(default_factory=list)   # reference examples/templates
    term_notes: list[dict[str, Any]] = Field(default_factory=list)  # terminology definitions
