"""Workflow registry — built-in workflow definitions.

Provides the standard workflows:
  - reflection: schema_linking → gen_sql → execute_sql → reflect → output
  - fixed:      schema_linking → gen_sql → execute_sql → output (no reflection)
  - empty:      output only (pass-through)
"""

from __future__ import annotations

from trove.workflow.engine import WorkflowDefinition
from trove.workflow.nodes.schema_linking import SchemaLinkingNode
from trove.workflow.nodes.gen_sql import GenSQLNode
from trove.workflow.nodes.execute_sql import ExecuteSQLNode
from trove.workflow.nodes.reflect import ReflectNode
from trove.workflow.nodes.output import OutputNode


class WorkflowRegistry:
    """Factory for built-in workflow definitions.

    Usage:
        reg = WorkflowRegistry()
        wf = reg.create("reflection")
        engine.register(wf)
    """

    @staticmethod
    def create(name: str) -> WorkflowDefinition:
        """Create a workflow definition by name.

        Args:
            name: One of "reflection", "fixed", "empty".

        Returns:
            A WorkflowDefinition with nodes and edges configured.

        Raises:
            ValueError: If the workflow name is unknown.
        """
        if name == "reflection":
            return WorkflowRegistry._reflection()
        elif name == "fixed":
            return WorkflowRegistry._fixed()
        elif name == "empty":
            return WorkflowRegistry._empty()
        else:
            raise ValueError(
                f"Unknown workflow: '{name}'. Available: reflection, fixed, empty"
            )

    @staticmethod
    def list_available() -> list[str]:
        return ["reflection", "fixed", "empty"]

    # ── Workflow builders ────────────────────────────────

    @staticmethod
    def _reflection() -> WorkflowDefinition:
        """reflection: Full pipeline with self-correction.

        schema_linking → gen_sql → execute_sql → reflect → output
        reflect may retry gen_sql up to 2 times.
        """
        nodes = [
            SchemaLinkingNode(),
            GenSQLNode(),
            ExecuteSQLNode(),
            ReflectNode(),
            OutputNode(),
        ]
        edges = [
            ("schema_linking", "gen_sql"),
            ("gen_sql", "execute_sql"),
            ("execute_sql", "reflect"),
            ("reflect", "output"),
        ]
        return WorkflowDefinition(
            name="reflection",
            nodes=nodes,
            edges=edges,
            default=True,
        )

    @staticmethod
    def _fixed() -> WorkflowDefinition:
        """fixed: Simple pipeline without reflection.

        schema_linking → gen_sql → execute_sql → output
        Lower latency and cost; no self-correction.
        """
        nodes = [
            SchemaLinkingNode(),
            GenSQLNode(),
            ExecuteSQLNode(),
            OutputNode(),
        ]
        edges = [
            ("schema_linking", "gen_sql"),
            ("gen_sql", "execute_sql"),
            ("execute_sql", "output"),
        ]
        return WorkflowDefinition(
            name="fixed",
            nodes=nodes,
            edges=edges,
        )

    @staticmethod
    def _empty() -> WorkflowDefinition:
        """empty: Pass-through — output echoes the input.

        Used for testing and debugging.
        """
        return WorkflowDefinition(
            name="empty",
            nodes=[OutputNode()],
        )
