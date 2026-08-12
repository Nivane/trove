"""SQL fixer — retry loop for SQL generation failures.

Coordinates between the gen_sql node, validator, and LLM
to implement the fix cycle: error → retry prompt → new SQL.
"""

from __future__ import annotations

from trove.core.logging import get_logger
from trove.services.sql.validator import SQLValidator, ValidationResult

logger = get_logger(__name__)


class SQLFixer:
    """Manages the SQL fix/retry process.

    Not an agentic node itself — it's a utility used by GenSQLNode
    to structure the fix cycle.
    """

    def __init__(
        self,
        max_retries: int = 3,
        dialect: str = "",
    ):
        self.max_retries = max_retries
        self.validator = SQLValidator(dialect=dialect)

    def should_retry(
        self,
        attempt: int,
        result: ValidationResult,
    ) -> bool:
        """Determine if another retry is warranted.

        Args:
            attempt: Current attempt number (0-indexed).
            result: The validation result.

        Returns:
            True if another retry should be attempted.
        """
        if attempt >= self.max_retries:
            return False
        if result.valid:
            return False
        # Only retry for parse/syntax errors, not semantic issues
        has_parse_error = any(
            "parse" in e.lower() or "syntax" in e.lower()
            for e in result.errors
        )
        return has_parse_error

    def classify_error(self, result: ValidationResult) -> str:
        """Classify an error for better retry prompts.

        Args:
            result: The validation result.

        Returns:
            Error classification: "syntax", "write_operation", "empty", "unknown".
        """
        if not result.errors:
            return "unknown"

        error_text = " ".join(result.errors).lower()

        if "empty" in error_text:
            return "empty"
        if "dangerous" in error_text or "write" in error_text or "drop" in error_text:
            return "write_operation"
        if "parse" in error_text or "syntax" in error_text:
            return "syntax"

        return "unknown"

    def build_fix_context(
        self,
        original_question: str,
        failed_sql: str,
        errors: list[str],
        schema_context: str,
    ) -> str:
        """Build comprehensive context for the fix prompt.

        Args:
            original_question: The user's original question.
            failed_sql: The invalid SQL.
            errors: Validation error messages.
            schema_context: Table schemas.

        Returns:
            A prompt string for the LLM to generate a fixed SQL.
        """
        parts = [
            "Fix the following SQL query.\n",
            f"Original question: {original_question}\n",
            f"Failed SQL:\n```sql\n{failed_sql}\n```\n",
            "Validation errors:",
        ]
        for err in errors:
            parts.append(f"  - {err}")

        parts.append("")
        if schema_context:
            parts.append(f"Schema:\n{schema_context}\n")

        parts.append("Generate ONLY the corrected SQL in a ```sql code block.")
        return "\n".join(parts)
