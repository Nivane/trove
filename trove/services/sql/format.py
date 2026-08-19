"""SQL pretty-printing for display (SQLGlot pretty generator)."""

from __future__ import annotations

from trove.core.logging import get_logger

logger = get_logger(__name__)


def format_sql(sql: str, dialect: str = "") -> str:
    """Pretty-print SQL for display.

    Falls back to the raw SQL unchanged when SQLGlot cannot parse it
    (display-only; never used for execution).
    """
    if not sql or not sql.strip():
        return sql
    try:
        import sqlglot
        from sqlglot import exp

        parsed = sqlglot.parse_one(
            sql,
            dialect=dialect or None,
            error_level=sqlglot.ErrorLevel.RAISE,
        )
        # sqlglot's tolerant parser "auto-corrects" garbage (e.g. "SELEC x")
        # into a bare expression — only pretty-print real statements.
        if not isinstance(parsed, (exp.Query, exp.DML, exp.DDL)):
            return sql
        return parsed.sql(pretty=True)
    except Exception:
        logger.debug("SQL pretty-print failed (%r); showing raw SQL", sql[:80])
        return sql