"""M:N 放宽:relationship fan_out=dedup 的编译期去重豁免。

默认 M:N 仍编译期拒绝(fan-out 行倍增);建模师显式声明 fan_out=dedup 后,
编译器把 from 侧包成 ``SELECT DISTINCT *`` 子查询消除行倍增。
"""
import pytest

from trove.services.semantic_layer.compiler import (
    CompileMiss,
    JoinResolver,
    SemanticCompiler,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)


def _field(name):
    return SemanticField(name=name, expression=name)


def _mn_model(fan_out: str):
    return SemanticModel(
        name="mn",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("account_id"), _field("amount"),
            ]),
            SemanticDataset(name="client", primary_key=["client_id"], fields=[
                _field("client_id"), _field("account_id"), _field("name"),
            ]),
        ],
        relationships=[
            SemanticRelationship(
                "loan_client", "client", "loan",
                from_columns=["account_id"], to_columns=["account_id"],
                cardinality="M:N", fan_out=fan_out,
            ),
        ],
        metrics=[
            SemanticMetric("total_loan", "SUM(loan.amount)", datasets=["loan"]),
        ],
    )


def _compile(model, plan, matched):
    return SemanticCompiler(model).compile_detailed(plan, matched)


def test_mn_default_still_rejected():
    """未声明 fan_out 的 M:N → 编译期拒绝(行倍增不静默放行)。"""
    model = _mn_model(fan_out="")
    plan = {
        "tables": ["loan", "client"],
        "aggregation": "total_loan",
        "answer_columns": ["client.name", "total_loan"],
    }
    result = _compile(model, plan, ["loan", "client"])
    assert isinstance(result, CompileMiss)
    assert result.reason == "fan_out"


def test_mn_dedup_join_side():
    """fan_out=dedup:M:N 的 from 侧(client)在 JOIN 位 → 去重子查询。"""
    model = _mn_model(fan_out="dedup")
    plan = {
        "tables": ["loan", "client"],
        "aggregation": "total_loan",
        "answer_columns": ["client.name", "total_loan"],
    }
    result = _compile(model, plan, ["loan", "client"])
    assert not isinstance(result, CompileMiss)
    sql = result.sql
    assert "FROM loan" in sql
    assert "JOIN (SELECT DISTINCT * FROM client) AS client" in sql
    assert "ON client.account_id = loan.account_id" in sql


def test_mn_dedup_anchor_side():
    """M:N 的 from 侧作锚表(FROM 位)→ FROM 本身去重,投影列仍可解析。"""
    model = _mn_model(fan_out="dedup")
    plan = {
        "tables": ["client", "loan"],
        "aggregation": "total_loan",
        "answer_columns": ["loan.amount", "total_loan"],
    }
    # 锚 = metric 数据集 loan → JOIN client(已覆盖);再测 client 为锚:
    # 让 metric 挂到 client,锚变 client,FROM 去重。
    model.metrics = [
        SemanticMetric("client_count", "COUNT(client.client_id)", datasets=["client"]),
    ]
    plan = {
        "tables": ["client", "loan"],
        "aggregation": "client_count",
        "answer_columns": ["loan.amount", "client_count"],
    }
    result = _compile(model, plan, ["client", "loan"])
    assert not isinstance(result, CompileMiss)
    sql = result.sql
    assert "FROM (SELECT DISTINCT * FROM client) AS client" in sql
    assert "JOIN loan ON client.account_id = loan.account_id" in sql


def test_mn_bridge_unsupported():
    """bridge 豁免未实现 → 保守拒绝(不产行倍增 SQL)。"""
    model = _mn_model(fan_out="bridge:client_agg")
    plan = {
        "tables": ["loan", "client"],
        "aggregation": "total_loan",
        "answer_columns": ["client.name", "total_loan"],
    }
    result = _compile(model, plan, ["loan", "client"])
    assert isinstance(result, CompileMiss)
    assert result.reason == "fan_out"


def test_resolver_reports_dedup_edges():
    model = _mn_model(fan_out="dedup")
    res = JoinResolver(model).resolve(["loan", "client"], root="loan", needed={"loan", "client"})
    assert res.fan_out is False
    assert [e.from_ for e in res.dedup_edges] == ["client"]
