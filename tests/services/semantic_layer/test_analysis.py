"""Window-function analysis compilation tests (B6/B7).

plan.analysis(share/running_total/mom/yoy/pct_change/rank) 把聚合核心包成
窗口查询。断言编译 SQL 的结构 + 用内存 SQLite 真实执行验证数值正确性。
"""

from __future__ import annotations

import sqlite3

import pytest

from trove.services.semantic_layer.compiler import CompileMiss, SemanticCompiler
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)


def _field(name, datatype=None):
    return SemanticField(name=name, expression=name, datatype=datatype)


def _demo_model():
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("account_id"), _field("amount"),
                _field("status"), _field("date", "Date"),
            ]),
            SemanticDataset(name="account", primary_key=["account_id"], fields=[
                _field("account_id"), _field("district_id"), _field("frequency"),
            ]),
            SemanticDataset(name="district", primary_key=["district_id"], fields=[
                _field("district_id"), _field("A3"),
            ]),
        ],
        relationships=[
            SemanticRelationship("loan_to_account", "loan", "account",
                                 from_columns=["account_id"], to_columns=["account_id"],
                                 cardinality="1:N"),
            SemanticRelationship("account_to_district", "account", "district",
                                 from_columns=["district_id"], to_columns=["district_id"],
                                 cardinality="1:N"),
        ],
        metrics=[
            SemanticMetric("number of loan records", "COUNT(loan.loan_id)", datasets=["loan"]),
            SemanticMetric("total_loan_amount", "SUM(loan.amount)", datasets=["loan"]),
        ],
    )


def _compile(plan, matched=None, model=None):
    return SemanticCompiler(model or _demo_model()).compile_detailed(
        plan, matched or ["loan", "account", "district"])


def _sql(res):
    assert not isinstance(res, CompileMiss), f"unexpected MISS: {res}"
    return res.sql


def _exec(sql):
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE loan (loan_id INTEGER, account_id INTEGER, amount REAL,
                           status TEXT, date TEXT);
        CREATE TABLE district (district_id INTEGER, A3 TEXT);
        CREATE TABLE account (account_id INTEGER, district_id INTEGER);
        INSERT INTO district VALUES (1,'East'),(2,'West');
        INSERT INTO account VALUES (10,1),(20,1),(30,2);
        INSERT INTO loan VALUES
          (1,10,100,'A','2024-01-15'),(2,20,200,'A','2024-02-15'),
          (3,30,300,'A','2024-03-15'),(4,10,150,'A','2024-04-15'),
          (5,20,50,'A','2024-05-15');
    """)
    return con.execute(sql).fetchall()


def _base_agg(dim_cols=None, time_grain=None):
    plan = {
        "tables": ["loan", "account", "district"],
        "aggregation": "sum(loan.amount)",
        "answer_columns": (dim_cols or ["district.A3"]) + ["sum(loan.amount)"],
    }
    if time_grain:
        plan["time_grain"] = time_grain
    return plan


class TestShare:
    def test_share_sql_shape(self):
        plan = _base_agg()
        plan["analysis"] = {"type": "share"}
        sql = _sql(_compile(plan))
        assert "SUM(" in sql and "OVER" in sql
        assert "NULLIF" in sql
        assert "AS share" in sql

    def test_share_executes_with_correct_values(self):
        plan = _base_agg()
        plan["analysis"] = {"type": "share"}
        res = _exec(_sql(_compile(plan)))
        # East=500, West=300 → shares 0.625 / 0.375
        assert res == [("East", 500.0, 0.625), ("West", 300.0, 0.375)]

    def test_share_partition_by(self):
        plan = _base_agg(dim_cols=["district.A3", "account.frequency"])
        plan["answer_columns"] = ["district.A3", "account.frequency", "sum(loan.amount)"]
        plan["analysis"] = {"type": "share", "partition_by": ["district.A3"]}
        sql = _sql(_compile(plan))
        assert "PARTITION BY" in sql


class TestRunningTotal:
    def test_running_total_executes(self):
        plan = _base_agg(dim_cols=["loan.date"], time_grain={"field": "loan.date", "grain": "month"})
        plan["analysis"] = {"type": "running_total"}
        sql = _sql(_compile(plan))
        res = _exec(sql)
        assert res[-1][2] == 800.0  # 累计 = 全量

    def test_running_total_needs_time(self):
        plan = _base_agg()
        plan["analysis"] = {"type": "running_total"}
        miss = _compile(plan)
        assert isinstance(miss, CompileMiss) and miss.reason == "analysis_time_required"


class TestMomYoyPct:
    def _trend_plan(self, atype):
        plan = _base_agg(dim_cols=["loan.date"], time_grain={"field": "loan.date", "grain": "month"})
        plan["analysis"] = {"type": atype}
        return plan

    def test_mom_executes(self):
        res = _exec(_sql(_compile(self._trend_plan("mom"))))
        assert res[0][2] is None  # 首期无环比
        assert res[1][2] == 100.0

    def test_yoy_uses_grain_lag(self):
        sql = _sql(_compile(self._trend_plan("yoy")))
        assert "LAG(_c1, 12)" in sql

    def test_yoy_quarter_lag(self):
        plan = self._trend_plan("yoy")
        plan["time_grain"] = {"field": "loan.date", "grain": "quarter"}
        sql = _sql(_compile(plan))
        assert "LAG(_c1, 4)" in sql

    def test_pct_change_executes(self):
        res = _exec(_sql(_compile(self._trend_plan("pct_change"))))
        assert res[0][2] is None
        assert res[1][2] == 1.0  # (200-100)/100


class TestRank:
    def test_rank_with_limit(self):
        plan = _base_agg()
        plan["analysis"] = {"type": "rank"}
        plan["limit"] = 1
        sql = _sql(_compile(plan))
        assert "RANK() OVER" in sql
        assert "LIMIT 1" in sql
        res = _exec(sql)
        assert res == [("East", 500.0, 1)]

    def test_rank_without_limit(self):
        plan = _base_agg()
        plan["analysis"] = {"type": "rank"}
        sql = _sql(_compile(plan))
        res = _exec(sql)
        assert res == [("East", 500.0, 1), ("West", 300.0, 2)]


class TestLimit:
    def test_plain_limit_with_ordering(self):
        plan = _base_agg()
        plan["ordering"] = "district.A3 desc"
        plan["limit"] = 1
        sql = _sql(_compile(plan))
        assert "LIMIT 1" in sql and "ORDER BY" in sql

    def test_plain_limit_without_ordering_miss(self):
        plan = _base_agg()
        plan["limit"] = 1
        miss = _compile(plan)
        assert isinstance(miss, CompileMiss) and miss.reason == "limit_without_order"


class TestAnalysisMiss:
    def test_unsupported_type(self):
        plan = _base_agg()
        plan["analysis"] = {"type": "pivot"}
        miss = _compile(plan)
        assert isinstance(miss, CompileMiss) and miss.reason == "analysis_unsupported_type"

    def test_unknown_metric(self):
        plan = _base_agg()
        plan["analysis"] = {"type": "share", "metric": "nonexistent"}
        miss = _compile(plan)
        assert isinstance(miss, CompileMiss) and miss.reason == "analysis_metric_unknown"

    def test_multiple_metrics_analysis_miss(self):
        plan = _base_agg()
        plan["answer_columns"] = ["district.A3", "sum(loan.amount)", "count(loan.loan_id)"]
        plan["analysis"] = {"type": "share"}
        miss = _compile(plan)
        assert isinstance(miss, CompileMiss) and miss.reason == "analysis_metric_unknown"

    def test_unresolved_order(self):
        plan = _base_agg(dim_cols=["loan.date"], time_grain={"field": "loan.date", "grain": "month"})
        plan["analysis"] = {"type": "mom", "order_by": "loan.nonexistent"}
        miss = _compile(plan)
        assert isinstance(miss, CompileMiss) and miss.reason == "analysis_order_unresolved"

    def test_unresolved_partition(self):
        plan = _base_agg()
        plan["analysis"] = {"type": "rank", "partition_by": ["loan.bogus"]}
        miss = _compile(plan)
        assert isinstance(miss, CompileMiss) and miss.reason == "analysis_partition_unresolved"

    def test_analysis_guardrail_passes(self):
        """分析产物仍过 validate_compiled_sql 守门(不引用连接树外表)。"""
        from trove.services.semantic_layer.compiler import validate_compiled_sql

        plan = _base_agg()
        plan["analysis"] = {"type": "share"}
        res = _compile(plan)
        sql = _sql(res)
        violations = validate_compiled_sql(sql, _demo_model(), ["loan", "account", "district"])
        assert violations == []
