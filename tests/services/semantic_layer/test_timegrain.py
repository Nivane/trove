"""Dialect-aware time-grain bucketing: 4 dialects × 5 grains 精确字符串。"""
import pytest

from trove.services.semantic_layer.timegrain import date_trunc

E = "loan.date"


@pytest.mark.parametrize("grain,expected", [
    ("year", "strftime('%Y', loan.date)"),
    ("quarter", "printf('%04d-Q%d', CAST(strftime('%Y', loan.date) AS INTEGER), "
                "(CAST(strftime('%m', loan.date) AS INTEGER) + 2) / 3)"),
    ("month", "strftime('%Y-%m', loan.date)"),
    ("week", "strftime('%Y-W%W', loan.date)"),
    ("day", "strftime('%Y-%m-%d', loan.date)"),
])
def test_sqlite(grain, expected):
    assert date_trunc(E, grain, "sqlite") == expected


@pytest.mark.parametrize("grain,expected", [
    ("year", "DATE_FORMAT(loan.date, '%Y')"),
    ("quarter", "CONCAT(YEAR(loan.date), '-Q', QUARTER(loan.date))"),
    ("month", "DATE_FORMAT(loan.date, '%Y-%m')"),
    ("week", "DATE_FORMAT(loan.date, '%x-W%v')"),
    ("day", "DATE_FORMAT(loan.date, '%Y-%m-%d')"),
])
def test_mysql(grain, expected):
    assert date_trunc(E, grain, "mysql") == expected


@pytest.mark.parametrize("grain,expected", [
    ("year", "date_trunc('year', loan.date)"),
    ("quarter", "date_trunc('quarter', loan.date)"),
    ("month", "date_trunc('month', loan.date)"),
    ("week", "date_trunc('week', loan.date)"),
    ("day", "date_trunc('day', loan.date)"),
])
def test_duckdb(grain, expected):
    assert date_trunc(E, grain, "duckdb") == expected


@pytest.mark.parametrize("grain,expected", [
    ("year", "toStartOfYear(loan.date)"),
    ("quarter", "toStartOfQuarter(loan.date)"),
    ("month", "toStartOfMonth(loan.date)"),
    ("week", "toStartOfWeek(loan.date)"),
    ("day", "toStartOfDay(loan.date)"),
])
def test_clickhouse(grain, expected):
    assert date_trunc(E, grain, "clickhouse") == expected


@pytest.mark.parametrize("grain", ["year", "quarter", "month", "week", "day"])
def test_unknown_dialect_falls_back_to_duckdb_style(grain):
    assert date_trunc(E, grain, "oracle") == f"date_trunc('{grain}', loan.date)"
    assert date_trunc(E, grain, "") == f"date_trunc('{grain}', loan.date)"
