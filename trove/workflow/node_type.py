"""Node type enum — the registry key for workflow nodes.

Each node type is a unique enum value used for:
  - Registration in the factory mapping
  - Workflow DAG definitions
  - Serialization/deserialization
"""

from __future__ import annotations

from enum import Enum


class NodeType(Enum):
    """All node types available in the workflow engine.

    Control nodes (no LLM):
      PARALLEL, SELECTION, SUBWORKFLOW

    Agentic nodes (with LLM loop):
      SCHEMA_LINKING, GEN_SQL, EXECUTE_SQL, REFLECT, OUTPUT
    """

    # ── Control nodes ────────────────────────────────────
    PARALLEL = "parallel"
    SELECTION = "selection"
    SUBWORKFLOW = "subworkflow"

    # ── Agentic / action nodes ───────────────────────────
    SCHEMA_LINKING = "schema_linking"
    GEN_SQL = "gen_sql"
    EXECUTE_SQL = "execute_sql"
    REFLECT = "reflect"
    OUTPUT = "output"

    # ── Extended nodes (available for future use) ─────────
    DATE_PARSER = "date_parser"
    SEARCH_METRICS = "search_metrics"
    SQL_SUMMARY = "sql_summary"
    FIX = "fix"
