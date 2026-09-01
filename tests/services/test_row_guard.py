"""EXPLAIN row-cost guard tests (B1)."""

from __future__ import annotations

from trove.core.types import QueryResult
from trove.services.sql.row_guard import estimate_max_rows, is_over_limit


def _res(columns, rows):
    return QueryResult(columns=columns, rows=rows)


class TestEstimateMaxRows:
    def test_postgres_plan_rows(self):
        r = _res(["QUERY PLAN"], [
            ("Aggregate  (cost=... rows=1 width=...)",),
            ("  ->  Seq Scan on loan  (cost=... rows=1000000 width=24)",),
        ])
        assert estimate_max_rows("postgres", r) == 1000000

    def test_postgresql_alias(self):
        r = _res(["QUERY PLAN"], [("Seq Scan on loan (cost=0 rows=42)",)])
        assert estimate_max_rows("postgresql", r) == 42

    def test_mysql_tabular_rows_column(self):
        r = _res(["id", "table", "rows"], [("1", "loan", 500000), ("1", "district", 100)])
        assert estimate_max_rows("mysql", r) == 500000

    def test_mysql_missing_rows_column_fail_open(self):
        r = _res(["id", "table"], [("1", "loan")])
        assert estimate_max_rows("mysql", r) is None

    def test_doris_tabular_rows_column(self):
        """Doris EXPLAIN 复用 MySQL 表格形态(rows 列)。"""
        r = _res(["id", "table", "rows"], [("1", "loan", 900000), ("1", "district", 100)])
        assert estimate_max_rows("doris", r) == 900000

    def test_duckdb_cardinality(self):
        r = _res(["physical_plan"], [("Pipeline   Cardinality: 8000000",)])
        assert estimate_max_rows("duckdb", r) == 8000000

    def test_duckdb_ec(self):
        r = _res(["physical_plan"], [("EC: 12000",)])
        assert estimate_max_rows("duckdb", r) == 12000

    def test_sqlite_fail_open(self):
        r = _res(["detail"], [("SCAN loan",)])
        assert estimate_max_rows("sqlite", r) is None

    def test_unknown_dialect_fail_open(self):
        r = _res(["x"], [("whatever",)])
        assert estimate_max_rows("oracle", r) is None


class TestIsOverLimit:
    def test_over_limit_blocks(self):
        r = _res(["rows"], [(99999999,)])
        assert is_over_limit("mysql", r, 50_000_000) is True

    def test_under_limit_allows(self):
        r = _res(["rows"], [(100,)])
        assert is_over_limit("mysql", r, 50_000_000) is False

    def test_unparseable_fail_open(self):
        r = _res(["detail"], [("SCAN loan",)])
        assert is_over_limit("sqlite", r, 1) is False
