"""SemanticManager OSSIE 序列化测试(metric type round-trip)。"""
from trove.services.semantic_layer.manage import _metric_payload_to_ossie


def test_metric_payload_carries_type():
    out = _metric_payload_to_ossie(
        "avg_per_loan",
        {"expression": "total_loan_amount / COUNT(loan.loan_id)", "type": "derived"},
        dialect="sqlite",
    )
    assert out["type"] == "derived"
    assert out["expression"]["dialects"][0]["expression"].startswith("total_loan_amount")


def test_metric_payload_without_type_omits_key():
    out = _metric_payload_to_ossie(
        "total_loan_amount", {"expression": "SUM(loan.amount)"}, dialect="sqlite")
    assert "type" not in out
