"""GenSQL node — generates SQL from natural language.

An AgenticNode that:
1. Builds a prompt with schema context + user question
2. Calls the LLM to generate SQL
3. Validates the SQL via SQLGlot
4. Retries on validation failure (up to max_retries)
"""

from __future__ import annotations

import re

from trove.core.types import NodeStatus, WorkflowContext
from trove.core.errors import SQLGenerationError
from trove.core.logging import get_logger
from trove.workflow.node import AgenticNode, NodeResult, LLMLoopConfig
from trove.workflow.node_type import NodeType

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


class GenSQLNode(AgenticNode):
    """Generate SQL from a natural language question.

    Internal loop:
      1. Build prompt (system + schema + user question)
      2. Call LLM → extract SQL
      3. Validate SQL (SQLGlot)
      4. If invalid → add errors to prompt → goto 2 (max max_retries times)
    """

    node_type = NodeType.GEN_SQL

    def __init__(
        self,
        name: str = "gen_sql",
        max_retries: int = 3,
        dialect: str = "sqlite",
    ):
        config = LLMLoopConfig(
            max_rounds=max_retries,
            system_prompt=SQL_GENERATION_SYSTEM_PROMPT,
        )
        super().__init__(name, config)
        self.max_retries = max_retries
        self.dialect = dialect

    async def execute(self, ctx: WorkflowContext) -> NodeResult:
        """Generate SQL with retry-on-failure.

        Uses the schema context from the upstream schema_linking node.
        """
        # Get schema context from previous node
        schema_context = ""
        if hasattr(ctx, '_node_data'):
            sl_data = ctx._node_data.get("schema_linking", {})  # type: ignore[attr-defined]
            schema_context = sl_data.get("schema_context", "")

        # Detect dialect from config
        datasource_default = getattr(ctx.config, '_datasource_default', '')
        if datasource_default:
            registry = getattr(ctx.config, '_connector_registry', None)
            if registry:
                try:
                    adapter = await registry.get()
                    self.dialect = adapter.dialect()
                except Exception:
                    pass

        question = ctx.user_message.content

        # Build initial prompt
        prompt = self._build_sql_prompt(question, schema_context, self.dialect)

        # Retry loop
        sql = ""
        last_errors: list[str] = []

        for attempt in range(self.max_retries):
            # Build prompt (include fix instructions on retry)
            if attempt == 0:
                final_prompt = prompt
            else:
                final_prompt = self._build_fix_prompt(sql, last_errors)

            try:
                response = await self._call_llm(ctx, final_prompt)
                sql = self._extract_sql(response)

                if not sql:
                    last_errors = ["LLM returned empty SQL"]
                    continue

                # Validate
                valid, errors = await self._validate_sql(sql, ctx)
                if valid:
                    return NodeResult(
                        node_name=self.name,
                        status=NodeStatus.SUCCESS,
                        data={
                            "sql": sql,
                            "attempts": attempt + 1,
                            "dialect": self.dialect,
                        },
                        metadata={"token_usage_prompt": self._count_tokens(final_prompt)},
                    )

                last_errors = errors
                logger.debug("SQL validation failed (attempt %d): %s", attempt + 1, errors)

            except Exception as e:
                last_errors = [str(e)]
                logger.warning("SQL generation attempt %d error: %s", attempt + 1, e)

        return NodeResult(
            node_name=self.name,
            status=NodeStatus.ERROR,
            error=SQLGenerationError(
                message=f"SQL generation failed after {self.max_retries} attempts",
                sql=sql,
                validation_errors=last_errors,
            ),
            data={"sql": sql, "errors": last_errors, "attempts": self.max_retries},
        )

    # ── Prompt builders ───────────────────────────────────

    def _build_sql_prompt(self, question: str, schema_context: str, dialect: str) -> str:
        """Build the initial SQL generation prompt."""
        parts = [
            f"Target SQL dialect: {dialect}",
            "",
            "Database schema:",
            schema_context or "(No schema information available - generate a best-effort query)",
            "",
            "Question:",
            question,
            "",
            "Generate the SQL query to answer this question:",
        ]
        return "\n".join(parts)

    def _build_fix_prompt(self, sql: str, errors: list[str]) -> str:
        """Build a fix prompt when validation fails."""
        return SQL_FIX_PROMPT.format(
            sql=sql,
            errors="\n".join(f"- {e}" for e in errors),
        )

    # ── SQL extraction ───────────────────────────────────

    def _extract_sql(self, response: str) -> str:
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

    # ── Validation ───────────────────────────────────────

    async def _validate_sql(
        self,
        sql: str,
        ctx: WorkflowContext,
    ) -> tuple[bool, list[str]]:
        """Validate SQL using SQLGlot.

        Returns:
            (is_valid, list_of_error_strings)
        """
        try:
            import sqlglot
            from sqlglot import exp

            errors = []

            try:
                # Pass the dialect as a string — resolving it via
                # getattr(sqlglot.dialects, ...) yields a module object
                # (only once submodules are loaded), which parse() rejects.
                parsed = sqlglot.parse(
                    sql, dialect=self.dialect,
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

    def _count_tokens(self, text: str) -> int:
        """Rough token count."""
        return len(text) // 4
