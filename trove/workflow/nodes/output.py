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

from trove.core.i18n import L
from trove.services.sql.format import format_sql
from trove.workflow.state import WorkflowState


async def output(state: WorkflowState) -> dict[str, Any]:
    """Format results into Markdown from the workflow state."""
    question = state.question

    if state.clarification_question:
        response = (
            "## Clarification\n\n"
            f"**Question**: {question}\n\n"
            f"{state.clarification_question}\n"
        )
        return {"final_response": response}

    if state.intent_answer:
        return {"final_response": state.intent_answer}

    lang = state.lang

    if state.error:
        response = (
            f"## {L(lang, 'Answer', 'Answer')}\n\n"
            f"**{L(lang, 'Question', 'Question')}**: {question}\n\n"
            f"**{L(lang, 'Error', 'Error')}**: {state.error}\n"
        )
        return {"final_response": response}

    parts = [f"## {L(lang, 'Answer', 'Answer')}\n"]

    # Question
    parts.append(f"**{L(lang, 'Question', 'Question')}**: {question}\n")

    # SQL
    if state.sql:
        parts.append(f"### {L(lang, 'Generated SQL', 'Generated SQL')}\n")
        parts.append(f"```sql\n{format_sql(state.sql, state.dialect)}\n```\n")

    # Results
    if state.columns:
        parts.append(L(lang, f"### Results ({state.row_count} rows)\n", f"### Results ({state.row_count} rows)\n"))

        # Build a markdown table
        parts.append("| " + " | ".join(state.columns) + " |")
        parts.append("| " + " | ".join("---" for _ in state.columns) + " |")

        for row in state.rows[:20]:  # Show first 20 rows
            parts.append("| " + " | ".join(str(cell) for cell in row) + " |")

        if state.row_count > 20:
            parts.append(f"\n*... and {state.row_count - 20} more rows*\n")

    elif state.row_count == 0:
        parts.append(L(lang, "**Result**: Query returned zero rows.\n", "**Result**: Query returned zero rows.\n"))
    else:
        # No execution data — this is the "empty" workflow case
        parts.append(L(lang, "(No query executed)\n", "(No query executed)\n"))

    # Reflection
    if state.verdict and state.verdict != "OK":
        parts.append(f"\n**Assessment**: {state.verdict}")
        if state.reason:
            parts.append(f" — {state.reason}")
        parts.append("\n")

    # Metadata
    if state.execution_time_ms:
        parts.append(f"\n---\n*Execution time: {state.execution_time_ms:.0f}ms*")

    # Multi-candidate disagreement → low-confidence note
    if not state.consensus:
        parts.append(L(lang, "\n*Confidence: low (candidate SQLs disagreed)*\n", "\n*Confidence: low (candidate SQLs disagreed)*\n"))

    # Knowledge base usage
    if state.kb_hits:
        term_parts = [
            f"{h['term']} → {h['mapping']}"
            for h in state.kb_hits
            if h.get("kind") == "term"
        ]
        example_count = sum(1 for h in state.kb_hits if h.get("kind") == "example")
        template_count = sum(1 for h in state.kb_hits if h.get("kind") == "template")
        segments = []
        if term_parts:
            segments.append(", ".join(term_parts))
        if example_count:
            label = "example" if example_count == 1 else "examples"
            segments.append(f"{example_count} {label} used")
        if template_count:
            segments.append(f"{template_count} template used (deterministic fast path)")
        parts.append(L(lang, f"\n*Knowledge base: {' | '.join(segments)}*\n", f"\n*Knowledge base: {' | '.join(segments)}*\n"))

    response = "\n".join(parts)

    return {"final_response": response}
