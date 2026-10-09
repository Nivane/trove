"""SemanticManager OSSIE 序列化测试(metric type round-trip)。"""
from pathlib import Path

import pytest

from trove.services.semantic_layer.manage import (
    _apply_dataset,
    _dataset_to_dict,
    _dump_yaml,
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


def test_dump_yaml_leaves_target_untouched_when_replace_fails(tmp_path, monkeypatch):
    """替换失败 = 目标逐字节保持原样 + 无临时文件残留。

    截断式写入（``path.write_text``）在写中途失败会留下半份 YAML，下一次
    ``_load_yaml`` 只看到半个字典；原子替换是 I5「取齐三份内容」之后仍
    存在的最后一道保护。失败方向必须钉住：注入的 ``os.replace`` 若根本没
    被调用（旧实现），这个用例会以「没有抛异常」红掉 —— 正是要红的原因。
    """
    target = tmp_path / "semantics.yml"
    target.write_text("datasets: []\n", encoding="utf-8")
    before = target.read_bytes()

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        _dump_yaml(target, {"datasets": [{"name": "loan"}]})
    assert target.read_bytes() == before
    # 临时文件必须被清理（同目录 mkstemp，残留会污染 KB 目录）
    assert [p.name for p in tmp_path.iterdir()] == ["semantics.yml"]


def test_dump_yaml_cleanup_failure_does_not_mask_the_original_error(
        tmp_path, monkeypatch):
    """unlink 自己失败时仍报真正的失败原因 —— 清理是善后,不是新错误源。"""
    target = tmp_path / "semantics.yml"
    target.write_text("datasets: []\n", encoding="utf-8")

    def boom(*_a, **_kw):
        raise OSError("disk full")

    def unlink_boom(*_a, **_kw):
        raise OSError("cannot unlink")

    monkeypatch.setattr("os.replace", boom)
    monkeypatch.setattr(Path, "unlink", unlink_boom)
    with pytest.raises(OSError, match="disk full"):
        _dump_yaml(target, {"datasets": []})
