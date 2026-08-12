"""Output node — formats the final response as Markdown.

Combines the user question, generated SQL, execution results,
and reflection into a human-readable Markdown response.
"""

from __future__ import annotations

from trove.core.types import NodeStatus, WorkflowContext
from trove.core.logging import get_logger
from trove.workflow.node import Node, NodeResult
from trove.workflow.node_type import NodeType

logger = get_logger(__name__)


class OutputNode(Node):
    """Format the final response for the user.

    Reads data from upstream nodes (schema_linking, gen_sql,
    execute_sql, reflect) and produces a formatted Markdown
    response string.
    """

    node_type = NodeType.OUTPUT

    def __init__(self, name: str = "output"):
        super().__init__(name)

    async def execute(self, ctx: WorkflowContext) -> NodeResult:
        """Format results into Markdown.

        Args:
            ctx: Workflow context.

        Returns:
            NodeResult with data["response"] containing the Markdown.
        """
        question = ctx.user_message.content

        # Gather data from upstream nodes
        node_data = getattr(ctx, '_node_data', {})

        sl = node_data.get("schema_linking", {})
        gen = node_data.get("gen_sql", {})
        exe = node_data.get("execute_sql", {})
        ref = node_data.get("reflect", {})

        parts = [f"## Answer\n"]

        # Question
        parts.append(f"**Question**: {question}\n")

        # SQL
        sql = gen.get("sql", "") or exe.get("sql", "")
        if sql:
            parts.append("### Generated SQL\n")
            parts.append(f"```sql\n{sql}\n```\n")

        # Results
        columns = exe.get("columns", [])
        rows = exe.get("rows", [])
        row_count = exe.get("row_count", -1)

        if columns:
            parts.append(f"### Results ({row_count} rows)\n")

            # Build a markdown table
            parts.append("| " + " | ".join(columns) + " |")
            parts.append("| " + " | ".join("---" for _ in columns) + " |")

            for row in rows[:20]:  # Show first 20 rows
                parts.append("| " + " | ".join(str(cell) for cell in row) + " |")

            if row_count > 20:
                parts.append(f"\n*... and {row_count - 20} more rows*\n")

        elif row_count == 0:
            parts.append("**Result**: Query returned zero rows.\n")
        elif "error" in exe:
            parts.append(f"**Error**: {exe.get('error', 'Unknown error')}\n")
        else:
            # No execution data — this is the "empty" workflow case
            parts.append("(No query executed)\n")

        # Reflection
        verdict = ref.get("verdict", "")
        reason = ref.get("reason", "")
        if verdict and verdict != "OK":
            parts.append(f"\n**Assessment**: {verdict}")
            if reason:
                parts.append(f" — {reason}")
            parts.append("\n")

        # Metadata
        exec_time = exe.get("execution_time_ms", 0)
        attempts = gen.get("attempts", 1)
        if exec_time:
            parts.append(f"\n---\n*Execution time: {exec_time:.0f}ms | SQL attempts: {attempts}*")

        response = "\n".join(parts)

        return NodeResult(
            node_name=self.name,
            status=NodeStatus.SUCCESS,
            data={
                "response": response,
                "question": question,
                "sql": sql,
                "row_count": row_count,
            },
        )
