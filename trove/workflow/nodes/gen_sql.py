"""GenSQL nodes — generate SQL from natural language with a validate-retry loop.

The loop is composed as a subgraph (see workflow/graphs.py) from two node
functions built here:
  - generate: builds the prompt (initial or fix), calls the LLM, extracts SQL
  - validate: validates via SQLGlot; routes back to generate on failure

Pure helpers (prompt builders, SQL extraction, validation) live at module
level for direct unit testing.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.workflow.state import GenSQLState

logger = get_logger(__name__)

# ── Prompt Templates ─────────────────────────────────────

SQL_GENERATION_SYSTEM_PROMPT = """You are a SQL generation assistant. Your task is to translate natural language questions into accurate SQL queries.

Guidelines:
1. Generate ONLY the SQL query, nothing else.
2. Use the provided table schemas exactly as described.
3. Use standard SQL syntax that works with the target dialect.
4. If the question is ambiguous, make reasonable assumptions and note them.
5. Do NOT use INSERT, UPDATE, DELETE, DROP, or any write operations.
6. Always use proper quoting for table and column names when needed.
7. If you cannot generate a valid SQL query, explain why.

Output format:
```sql
SELECT ...
```
"""

SQL_FIX_PROMPT = """The following SQL query failed validation:

```sql
{sql}
```

Validation errors:
{errors}

Please fix the SQL query. Generate ONLY the corrected SQL.

```sql
SELECT ...
```
"""


def build_sql_prompt(
    question: str,
    schema_context: str,
    dialect: str,
    reflect_reason: str = "",
    error_feedback: str = "",
    few_shots: list[dict[str, Any]] | None = None,
    term_notes: list[dict[str, Any]] | None = None,
) -> str:
    """Build the initial SQL generation prompt.

    Knowledge base material (optional) is injected as:
      - Terminology: standard formulations for matched business terms
      - Reference examples: top-K similar questions with their SQL
    """
    parts = [
        f"Target SQL dialect: {dialect}",
        "",
        "Database schema:",
        schema_context or "(No schema information available - generate a best-effort query)",
        "",
    ]
    if term_notes:
        parts.append("Terminology (standard formulations):")
        for note in term_notes:
            line = f"- {note.get('term', '')} → {note.get('mapping', '')}"
            if note.get("definition"):
                line += f" — {note['definition']}"
            parts.append(line)
        parts.append("")
    if few_shots:
        parts.append("Reference examples (standard formulations for this data):")
        for shot in few_shots:
            parts.append(f"Q: {shot.get('question', '')}")
            parts.append(f"SQL: {shot.get('sql', '')}")
        parts.append("")
    parts += [
        "Question:",
        question,
        "",
    ]
    if reflect_reason:
        parts += [
            f"Note: a previous version of this query was rejected with: "
            f"{reflect_reason}. Please correct it.",
            "",
        ]
    if error_feedback:
        parts += [
            f"Note: the previous query failed during execution with: "
            f"{error_feedback}. Please correct the SQL.",
            "",
        ]
    parts.append("Generate the SQL query to answer this question:")
    return "\n".join(parts)


def build_fix_prompt(sql: str, errors: list[str]) -> str:
    """Build a fix prompt when validation fails."""
    return SQL_FIX_PROMPT.format(
        sql=sql,
        errors="\n".join(f"- {e}" for e in errors),
    )


def extract_sql(response: str) -> str:
    """Extract SQL from LLM response (handles markdown code blocks)."""
    # Try ```sql ... ``` block first
    match = re.search(r'```sql\s*\n(.*?)\n```', response, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # Try generic ``` ... ``` block
    match = re.search(r'```\s*\n(.*?)\n```', response, re.DOTALL)
    if match:
        return match.group(1).strip()

    # If no code block, return the raw response (strip common prefixes)
    text = response.strip()
    for prefix in ["SELECT", "WITH", "select", "with"]:
        idx = text.find(prefix)
        if idx >= 0:
            return text[idx:]

    return text


def validate_sql(sql: str, dialect: str) -> tuple[bool, list[str]]:
    """Validate SQL using SQLGlot.

    Returns:
        (is_valid, list_of_error_strings)
    """
    try:
        import sqlglot
        from sqlglot import exp

        errors = []

        try:
            parsed = sqlglot.parse(
                sql, dialect=dialect,
                error_level=sqlglot.ErrorLevel.RAISE,
            )
            statements = [p for p in parsed if p is not None]
            if not statements:
                errors.append("SQL could not be parsed (empty result)")
            elif len(statements) > 1:
                errors.append("Multiple SQL statements are not allowed")
            elif not isinstance(statements[0], exp.Query):
                # e.g. "SELEC * FORM t" parses into an Alias, not a query
                errors.append("Not a valid SQL query")
        except Exception as e:
            errors.append(f"Parse error: {e}")

        return len(errors) == 0, errors

    except ImportError:
        # sqlglot not installed — skip validation
        logger.warning("sqlglot not available; skipping SQL validation")
        return True, []


# ── Subgraph nodes ───────────────────────────────────────


def make_generate(
    llm: LLMGateway,
    config: AgentConfig,
) -> Callable[[GenSQLState], Awaitable[dict[str, Any]]]:
    """Build the generate node: prompt → LLM → extract SQL."""

    async def generate(state: GenSQLState) -> dict[str, Any]:
        if state.error:
            return {}

        if state.validation_errors:
            prompt = build_fix_prompt(state.sql, state.validation_errors)
        else:
            prompt = build_sql_prompt(
                question=state.question,
                schema_context=state.schema_context,
                dialect=state.dialect,
                reflect_reason=state.reflect_reason,
                error_feedback=state.error_feedback,
                few_shots=state.few_shots or None,
                term_notes=state.term_notes or None,
            )

        model = config.target or "openai/gpt-4o"
        response = await llm.chat(
            model=model,
            messages=[
                {"role": "system", "content": SQL_GENERATION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        sql = extract_sql(response)

        return {
            "sql": sql,
            "attempts": state.attempts + 1,
            "validation_errors": [],
        }

    return generate


def make_validate(
    max_retries: int = 3,
) -> Callable[[GenSQLState], Awaitable[dict[str, Any]]]:
    """Build the validate node: SQLGlot check, error bookkeeping, exhaustion."""

    async def validate(state: GenSQLState) -> dict[str, Any]:
        if state.error:
            return {}

        if not state.sql:
            errors = ["LLM returned empty SQL"]
        else:
            valid, errors = validate_sql(state.sql, state.dialect)
            if valid:
                return {"validation_errors": []}

        if state.attempts >= max_retries:
            return {
                "error": (
                    f"SQL generation failed after {state.attempts} attempts: "
                    + "; ".join(errors)
                ),
                "validation_errors": errors,
            }
        return {"validation_errors": errors}

    return validate
