"""SQL validator tests."""

import pytest

from trove.services.sql.validator import SQLValidator, ValidationResult, DANGEROUS_KEYWORDS


class TestSQLValidator:
    def test_valid_select(self):
        v = SQLValidator()
        result = v.validate("SELECT * FROM students")
        assert result.valid is True
        assert result.errors == []

    def test_valid_complex_query(self):
        v = SQLValidator()
        sql = """
        SELECT county, AVG(grade) as avg_grade
        FROM students
        WHERE grade > 60
        GROUP BY county
        ORDER BY avg_grade DESC
        """
        result = v.validate(sql)
        assert result.valid is True

    def test_empty_sql(self):
        v = SQLValidator()
        result = v.validate("")
        assert result.valid is False
        assert any("empty" in e.lower() for e in result.errors)

    def test_whitespace_sql(self):
        v = SQLValidator()
        result = v.validate("   \n\t ")
        assert result.valid is False

    def test_invalid_syntax(self):
        v = SQLValidator()
        result = v.validate("SELEC * FORM students")
        assert result.valid is False

    def test_write_operation_blocked(self):
        v = SQLValidator()
        result = v.validate("DELETE FROM students")
        assert result.valid is False
        assert any("Dangerous" in e for e in result.errors)

    def test_drop_blocked(self):
        v = SQLValidator()
        result = v.validate("DROP TABLE students")
        assert result.valid is False

    def test_insert_blocked(self):
        v = SQLValidator()
        result = v.validate("INSERT INTO students VALUES (1)")
        assert result.valid is False

    def test_write_check_disabled(self):
        v = SQLValidator(check_write_operations=False)
        result = v.validate("DELETE FROM students")
        assert result.valid is True

    def test_with_dialect(self):
        v = SQLValidator(dialect="sqlite")
        result = v.validate("SELECT sqlite_version()")
        assert result.valid is True

    def test_is_safe_select(self):
        v = SQLValidator()
        assert v.is_safe("SELECT * FROM t") is True
        assert v.is_safe("WITH x AS (SELECT 1) SELECT * FROM x") is True

    def test_is_safe_write_ops(self):
        v = SQLValidator()
        assert v.is_safe("DELETE FROM t") is False
        assert v.is_safe("DROP TABLE t") is False
        assert v.is_safe("UPDATE t SET a = 1") is False
        assert v.is_safe("INSERT INTO t VALUES (1)") is False
        assert v.is_safe("ALTER TABLE t ADD COLUMN b") is False
        assert v.is_safe("CREATE TABLE t2 (id INT)") is False

    def test_subquery_with_write_op(self):
        """Write operations inside subqueries should also be caught."""
        v = SQLValidator()
        # UPDATE inside parentheses
        assert v.is_safe("SELECT * FROM (UPDATE t SET a=1) x") is False


class TestValidationResult:
    def test_default(self):
        r = ValidationResult(valid=True)
        assert r.valid is True
        assert r.errors == []
        assert r.warnings == []


class TestDangerousKeywords:
    def test_contains_expected(self):
        for kw in ["DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "CREATE", "TRUNCATE"]:
            assert kw in DANGEROUS_KEYWORDS
