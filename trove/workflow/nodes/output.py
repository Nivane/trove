"""Output node — formats the final response as Markdown.

Combines the user question, generated SQL, execution results,
and reflection into a human-readable Markdown response.
On graceful degradation (state.error set) it formats a readable
error section instead.

Node shape: `async def output(state: WorkflowState) -> dict`
returns a partial state update.
"""

from __future__ import annotations

from typing import Any

from trove.workflow.state import WorkflowState


async def output(state: WorkflowState) -> dict[str, Any]:
    """Format results into Markdown from the workflow state."""
    question = state.question

    if state.error:
        response = (
            "## Answer\n\n"
            f"**Question**: {question}\n\n"
            f"**Error**: {state.error}\n"
        )
        return {"final_response": response}

    parts = [f"## Answer\n"]

    # Question
    parts.append(f"**Question**: {question}\n")

    # SQL
    if state.sql:
        parts.append("### Generated SQL\n")
        parts.append(f"```sql\n{state.sql}\n```\n")

    # Results
    if state.columns:
        parts.append(f"### Results ({state.row_count} rows)\n")

        # Build a markdown table
        parts.append("| " + " | ".join(state.columns) + " |")
        parts.append("| " + " | ".join("---" for _ in state.columns) + " |")

        for row in state.rows[:20]:  # Show first 20 rows
            parts.append("| " + " | ".join(str(cell) for cell in row) + " |")

        if state.row_count > 20:
            parts.append(f"\n*... and {state.row_count - 20} more rows*\n")

    elif state.row_count == 0:
        parts.append("**Result**: Query returned zero rows.\n")
    else:
        # No execution data — this is the "empty" workflow case
        parts.append("(No query executed)\n")

    # Reflection
    if state.verdict and state.verdict != "OK":
        parts.append(f"\n**Assessment**: {state.verdict}")
        if state.reason:
            parts.append(f" — {state.reason}")
        parts.append("\n")

    # Metadata
    if state.execution_time_ms:
        parts.append(f"\n---\n*Execution time: {state.execution_time_ms:.0f}ms*")

    response = "\n".join(parts)

    return {"final_response": response}
