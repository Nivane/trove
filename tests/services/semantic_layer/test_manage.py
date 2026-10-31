"""SemanticManager OSSIE 序列化测试(metric type round-trip)。"""
from pathlib import Path

import pytest

from trove.services.semantic_layer.manage import (
    _ACTION_LABELS,
    _DIFF_LABELS,
    _apply_dataset,
    _dataset_to_dict,
    _dump_yaml,
    _find_entity,
    _metric_payload_to_ossie,
    _model_to_dict,
    _raw_view,
    _relationship_to_dict,
)
from trove.services.semantic_layer.models import (
    Hierarchy,
    SemanticDataset,
    SemanticEntity,
    SemanticModel,
    SemanticRelationship,
)


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


# ── 对象视图声明:序列化 / 查找 / 视图 / 标签(E1)──────────────


def test_entity_serialized_with_three_keys():
    ds = SemanticDataset(name="loan", entity=SemanticEntity(
        display_name="贷款", natural_keys=["loan_id"], aliases=["借款"]))
    assert _dataset_to_dict(ds)["entity"] == {
        "display_name": "贷款", "natural_keys": ["loan_id"], "aliases": ["借款"]}


def test_absent_entity_serializes_as_none_not_empty_dict():
    """``entity: null``(没声明)与三键全空的字典在管理页是两件事。"""
    assert _dataset_to_dict(SemanticDataset(name="loan"))["entity"] is None


def test_relationship_display_name_serialized():
    r = SemanticRelationship(name="e", from_="a", to="b", display_name="属于")
    assert _relationship_to_dict(r)["display_name"] == "属于"
    assert _relationship_to_dict(
        SemanticRelationship(name="e", from_="a", to="b"))["display_name"] == ""


def test_model_dict_carries_hierarchies():
    m = SemanticModel(name="m", hierarchies=[
        Hierarchy(name="h", display_name="产品层级", dataset="loan",
                  levels=["product_class", "product_name"])])
    assert _model_to_dict(m)["hierarchies"] == [{
        "name": "h", "display_name": "产品层级", "dataset": "loan",
        "levels": ["product_class", "product_name"]}]
    # 键恒在(空模型 → []),形状不随声明变化
    assert _model_to_dict(SemanticModel(name="m"))["hierarchies"] == []


def test_dataset_metadata_upsert_preserves_entity_block():
    """仅改元数据的 upsert 不得抹掉手写的 entity 子块(N11 同款静默丢失)。"""
    model: dict = {"datasets": [{
        "name": "loan", "source": "loan", "fields": [],
        "entity": {"display_name": "贷款", "natural_keys": ["loan_id"]}}]}
    _apply_dataset(model, "upsert", "loan", {"description": "贷款表"}, None)
    assert model["datasets"][0]["entity"] == {
        "display_name": "贷款", "natural_keys": ["loan_id"]}


_LOOKUP_DOC = {"version": "0.2.0.dev0",
    "name": "m",
    "datasets": [
        {"name": "loan", "entity": {"display_name": "贷款"},
         "fields": [{"name": "amount"}]},
        {"name": "customer"},
    ],
    "hierarchies": [{"name": "h", "dataset": "loan", "levels": ["a"]}],
    "relationships": [{"name": "r", "from": "loan", "to": "customer"}],}


def test_find_entity_resolves_the_new_kinds():
    data = _LOOKUP_DOC
    # entity 的 kind 名 = 归属数据集名,返回的是原始子块(只读引用)
    assert _find_entity(data, "entity", "loan") == {"display_name": "贷款"}
    assert _find_entity(data, "hierarchy", "h") == {
        "name": "h", "dataset": "loan", "levels": ["a"]}
    assert _find_entity(data, "relationship", "r") == {
        "name": "r", "from": "loan", "to": "customer"}
    # 有数据集但没声明 entity → None(未声明);数据集不存在 → None
    assert _find_entity(data, "entity", "customer") is None
    assert _find_entity(data, "entity", "nope") is None
    assert _find_entity(data, "hierarchy", "nope") is None


def test_raw_view_entity_is_flat_three_keys():
    view = _raw_view("entity", {"display_name": "贷款",
                                "natural_keys": ["loan_id"], "aliases": []})
    assert view == {"display_name": "贷款", "natural_keys": ["loan_id"]}
    assert _raw_view("entity", {}) == {}  # 空值不落键


def test_raw_view_hierarchy_is_flat():
    view = _raw_view("hierarchy", {
        "name": "h", "display_name": "产品层级", "dataset": "loan",
        "levels": ["a", "b"]})
    assert view == {"name": "h", "display_name": "产品层级",
                    "dataset": "loan", "levels": ["a", "b"]}


def test_raw_view_relationship_includes_display_name_only_when_declared():
    declared = _raw_view("relationship", {
        "name": "r", "from": "loan", "to": "customer",
        "from_columns": ["cid"], "to_columns": ["cid"],
        "display_name": "属于"})
    assert declared["display_name"] == "属于"
    undeclared = _raw_view("relationship", {
        "name": "r", "from": "loan", "to": "customer",
        "from_columns": ["cid"], "to_columns": ["cid"]})
    assert "display_name" not in undeclared  # 空值不落键(未声明)


def test_diff_and_action_labels_cover_the_new_declarations():
    """diff 行标签与动作标签同步加条目 —— 不变量式断言,防漏改。"""
    for key in ("entity", "display_name", "hierarchies", "natural_keys",
                "aliases", "dataset", "levels", "fan_out"):
        assert key in _DIFF_LABELS, key
    for key in ("entity", "hierarchy", "relationship"):
        assert key in _ACTION_LABELS, key
