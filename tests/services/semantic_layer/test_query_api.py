"""Semantic query API builder: declarative metric/dimension → compiled SQL.

任一组件未解析到已声明模型条目 → SemanticQueryError(严格,不猜测)。
"""
import pytest

from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)
from trove.services.semantic_layer.query import (
    SemanticQuery,
    SemanticQueryError,
    build_and_compile,
)


def _field(name):
    return SemanticField(name=name, expression=name)


def _demo_model():
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("account_id"), _field("amount"),
                _field("status"), SemanticField(name="date", expression="date", datatype="Date"),
            ]),
            SemanticDataset(name="account", primary_key=["account_id"], fields=[
                _field("account_id"), _field("district_id"),
            ]),
            SemanticDataset(name="district", primary_key=["district_id"], fields=[
                _field("district_id"), _field("A3"),
            ]),
        ],
        relationships=[
            SemanticRelationship("loan_to_account", "loan", "account",
                                 from_columns=["account_id"],
                                 to_columns=["account_id"], cardinality="1:N"),
            SemanticRelationship("account_to_district", "account", "district",
                                 from_columns=["district_id"],
                                 to_columns=["district_id"], cardinality="1:N"),
        ],
        metrics=[
            SemanticMetric("number of loan records", "COUNT(loan.loan_id)",
                           datasets=["loan"]),
            SemanticMetric("total_loan_amount", "SUM(loan.amount)",
                           datasets=["loan"]),
        ],
    )


def test_metric_by_name_with_dimension_and_filter():
    query = SemanticQuery(
        metrics=["total_loan_amount"],
        dimensions=["district.A3"],
        filters=[{"field": "loan.status", "op": "=", "value": "A"}],
        order_by=[{"column": "district.A3", "direction": "asc"}],
        limit=10,
    )
    out = build_and_compile(_demo_model(), query, dialect="sqlite")
    assert "SELECT district.A3, SUM(loan.amount)" in out["sql"]
    assert "FROM loan" in out["sql"]
    assert "WHERE loan.status = 'A'" in out["sql"]
    assert "ORDER BY district.A3 ASC" in out["sql"]
    assert "LIMIT 10" in out["sql"]
    assert out["columns"] == ["A3", "total_loan_amount"]
    assert out["datasets"] == ["loan", "district"]


def test_metric_by_agg_signature():
    query = SemanticQuery(metrics=["sum(loan.amount)"])
    out = build_and_compile(_demo_model(), query)
    assert "SUM(loan.amount)" in out["sql"]
    assert out["columns"] == ["total_loan_amount"]


def test_multi_metric():
    query = SemanticQuery(metrics=[
        "number of loan records", "total_loan_amount"])
    out = build_and_compile(_demo_model(), query)
    assert "COUNT(loan.loan_id)" in out["sql"]
    assert "SUM(loan.amount)" in out["sql"]
    assert out["columns"] == ["number of loan records", "total_loan_amount"]


def test_time_grain():
    query = SemanticQuery(
        metrics=["number of loan records"],
        time_grain={"field": "loan.date", "grain": "year"},
    )
    out = build_and_compile(_demo_model(), query)
    assert "strftime('%Y', loan.date)" in out["sql"]
    assert out["columns"] == ["year", "number of loan records"]


def test_unknown_metric_rejected():
    with pytest.raises(SemanticQueryError):
        build_and_compile(_demo_model(), SemanticQuery(metrics=["ghost_metric"]))


def test_empty_metrics_rejected():
    with pytest.raises(SemanticQueryError, match="at least one metric"):
        build_and_compile(_demo_model(), SemanticQuery(metrics=[]))


def test_unknown_field_rejected():
    query = SemanticQuery(metrics=["total_loan_amount"], dimensions=["loan.ghost"])
    with pytest.raises(SemanticQueryError, match="field not declared"):
        build_and_compile(_demo_model(), query)


def test_ambiguous_bare_field_rejected():
    """裸列名跨数据集重复 → 拒绝,要求 dataset.field 限定。"""
    model = _demo_model()
    model.datasets.append(SemanticDataset(name="other", fields=[
        _field("amount")]))
    query = SemanticQuery(metrics=["total_loan_amount"], dimensions=["amount"])
    with pytest.raises(SemanticQueryError, match="ambiguous"):
        build_and_compile(model, query)


def test_bad_time_grain_rejected():
    query = SemanticQuery(
        metrics=["number of loan records"],
        time_grain={"field": "loan.date", "grain": "decade"},
    )
    with pytest.raises(SemanticQueryError, match="time grain"):
        build_and_compile(_demo_model(), query)
