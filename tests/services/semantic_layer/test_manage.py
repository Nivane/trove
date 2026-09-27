"""SemanticManager OSSIE 序列化测试(metric type round-trip)。"""
from trove.services.semantic_layer.manage import (
    _apply_dataset,
    _dataset_to_dict,
    _metric_payload_to_ossie,
)
from trove.services.semantic_layer.models import SemanticDataset


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


def test_dataset_row_filter_round_trip():
    """row_filter 经 draft payload 落盘,并经管理页序列化带出。"""
    model: dict = {"datasets": []}
    _apply_dataset(
        model, "upsert", "loan",
        {"source": "loan", "row_filter": "loan.status = 'A'"}, None)
    assert model["datasets"][0]["row_filter"] == "loan.status = 'A'"

    ds = SemanticDataset(name="loan", row_filter="loan.status = 'A'")
    assert _dataset_to_dict(ds)["row_filter"] == "loan.status = 'A'"


def test_dataset_row_filter_carried_over_on_metadata_upsert():
    """仅改元数据的 upsert 不得抹掉已声明的 row_filter(RLS 不可静默丢失)。"""
    model: dict = {"datasets": [
        {"name": "loan", "source": "loan", "fields": [],
         "row_filter": "loan.status = 'A'"}]}
    _apply_dataset(model, "upsert", "loan", {"description": "贷款"}, None)
    assert model["datasets"][0]["row_filter"] == "loan.status = 'A'"
