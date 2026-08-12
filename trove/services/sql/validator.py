"""SQL validation using SQLGlot.

Validates generated SQL for:
  - Syntax correctness
  - Dialect compliance
  - Basic safety (no write operations by default)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from trove.core.logging import get_logger

logger = get_logger(__name__)

# Keywords that indicate write operations
DANGEROUS_KEYWORDS = frozenset({
    "DROP", "DELETE", "INSERT", "UPDATE", "ALTER",
    "CREATE", "TRUNCATE", "GRANT", "REVOKE",
})


@dataclass
class ValidationResult:
    """Result of SQL validation."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dialect: str = ""


class SQLValidator:
    """Validate SQL queries for correctness and safety."""

    def __init__(
        self,
        dialect: str = "",
        check_write_operations: bool = True,
    ):
        """Initialize the validator.

        Args:
            dialect: Target SQL dialect (e.g. "sqlite", "postgres").
            check_write_operations: If True, flag write operations as errors.
        """
        self.dialect = dialect
        self.check_write_operations = check_write_operations

    def validate(self, sql: str) -> ValidationResult:
        """Validate a SQL string.

        Args:
            sql: The SQL to validate.

        Returns:
            ValidationResult with validity and error/warning lists.
        """
        errors: list[str] = []
        warnings: list[str] = []

        # Basic checks
        if not sql or not sql.strip():
            errors.append("SQL is empty")
            return ValidationResult(valid=False, errors=errors, dialect=self.dialect)

        sql_upper = sql.strip().upper()

        # Check for write operations
        if self.check_write_operations:
            for keyword in DANGEROUS_KEYWORDS:
                # Check for keyword at start or after whitespace/parenthesis
                import re
                pattern = r'(?:^|\s|\()' + re.escape(keyword) + r'(?:\s|$)'
                if re.search(pattern, sql_upper):
                    errors.append(
                        f"Dangerous operation: {keyword}. "
                        f"Write operations are not permitted for auto-generated SQL."
                    )

        # SQLGlot parsing — RAISE error level so hard syntax errors are
        # rejected instead of being silently "auto-corrected" by the
        # tolerant parser. Additionally require the statement to parse as
        # an actual query: malformed input like "SELEC * FORM t" parses
        # into an Alias/Column expression without raising.
        try:
            import sqlglot
            from sqlglot import exp

            try:
                if self.dialect:
                    parsed = sqlglot.parse(
                        sql, dialect=self.dialect,
                        error_level=sqlglot.ErrorLevel.RAISE,
                    )
                else:
                    parsed = sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.RAISE)

                statements = [p for p in parsed if p is not None]
                if not statements:
                    errors.append("SQL could not be parsed (empty or invalid)")
                elif len(statements) > 1:
                    errors.append("Multiple SQL statements are not allowed")
                elif not isinstance(statements[0], (exp.Query, exp.DML, exp.DDL)):
                    # Garbage like "SELEC * FORM t" parses into an Alias
                    # rather than a query or statement — reject it.
                    errors.append("Not a valid SQL query")
            except Exception as e:
                errors.append(f"Parse error: {e}")

        except ImportError:
            warnings.append("sqlglot not installed; skipping syntax validation")

        valid = len(errors) == 0
        return ValidationResult(
            valid=valid,
            errors=errors,
            warnings=warnings,
            dialect=self.dialect,
        )

    def is_safe(self, sql: str) -> bool:
        """Check if SQL contains only read operations.

        Args:
            sql: SQL to check.

        Returns:
            True if the SQL is read-only.
        """
        sql_upper = sql.strip().upper()
        for keyword in DANGEROUS_KEYWORDS:
            import re
            pattern = r'(?:^|\s|\()' + re.escape(keyword) + r'(?:\s|$)'
            if re.search(pattern, sql_upper):
                return False
        return True
