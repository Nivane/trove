"""SQL generation prompt templates and utilities.

Provides structured prompt building for the gen_sql node.
The actual LLM call happens in the node itself;
this service provides the prompt construction logic.
"""

from __future__ import annotations


class SQLGenerator:
    """Build prompts for SQL generation from natural language + schema context."""

    def __init__(self, dialect: str = "sqlite"):
        self.dialect = dialect

    def build_system_prompt(self) -> str:
        """Build the system prompt for SQL generation."""
        return (
            "You are a precise SQL generation assistant. "
            "Generate ONLY valid SQL queries that answer the user's question. "
            "Use the provided database schema exactly as described. "
            f"Target dialect: {self.dialect}. "
            "Do NOT use INSERT, UPDATE, DELETE, DROP, or any write operations. "
            "Wrap the SQL in a ```sql code block. "
            "If you are unsure, explain your assumptions in a SQL comment."
        )

    def build_user_prompt(
        self,
        question: str,
        schema_context: str,
        history_context: str = "",
    ) -> str:
        """Build the user prompt combining question, schema, and history.

        Args:
            question: The user's natural language question.
            schema_context: Table schemas from schema_linking.
            history_context: Optional relevant SQL history summaries.

        Returns:
            Formatted prompt string.
        """
        parts = []

        if history_context:
            parts.append("### Related Previous Queries\n")
            parts.append(history_context)
            parts.append("")

        parts.append("### Database Schema\n")
        parts.append(schema_context)
        parts.append("")

        parts.append("### Question\n")
        parts.append(question)
        parts.append("")

        parts.append("Generate the SQL query:")
        return "\n".join(parts)

    def build_fix_prompt(
        self,
        failed_sql: str,
        errors: list[str],
        schema_context: str,
    ) -> str:
        """Build a prompt for fixing a failed SQL query.

        Args:
            failed_sql: The SQL that failed validation.
            errors: List of validation error messages.
            schema_context: Table schemas for context.

        Returns:
            Formatted fix prompt.
        """
        parts = [
            "The following SQL query is invalid:\n",
            f"```sql\n{failed_sql}\n```\n",
            "### Errors\n",
        ]
        for i, err in enumerate(errors, 1):
            parts.append(f"{i}. {err}")
        parts.append("")

        if schema_context:
            parts.append("### Schema\n")
            parts.append(schema_context)
            parts.append("")

        parts.append("Please fix the SQL. Output ONLY the corrected SQL in a ```sql code block.")
        return "\n".join(parts)
