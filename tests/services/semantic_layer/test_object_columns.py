"""答案卡对象列单测(设计 §5.7 / FR-GRAPH-5)。

钉住四条纪律:

1. **唯一精确命中才建映射**:零命中(未声明实体/列名对不上)或**多实体
   同名字段撞车** → 该列不出现(宁缺不猜;唯一例外被精确命中时才建);
2. **显示名回退与对象图 D1 同一口径**:直接对 ``build_object_graph`` 的
   同一数据集断言相等 —— 两个消费面(对象图页/答案卡表头)给同一个名字;
3. **只加映射不动值**:产物是纯 dict,输入不被改动,列序 = 入参列序;
4. **边界不抛**:None 模型 / 空列 / 非 str 列 → 空 dict。
"""
from __future__ import annotations

from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticEntity,
    SemanticField,
    SemanticModel,
)
from trove.services.semantic_layer.object_columns import build_object_columns
from trove.services.semantic_layer.object_graph import build_object_graph


def _field(name: str) -> SemanticField:
    return SemanticField(name=name, expression=name)


def _model(*datasets: SemanticDataset) -> SemanticModel:
    return SemanticModel(name="demo", datasets=list(datasets))


# loan:声明了实体且给了显示名;customer:声明了实体但 display_name 缺省
# (回退数据集名);ledger:未声明实体。
LOAN = SemanticDataset(
    name="loan", source="loan", entity=SemanticEntity(display_name="贷款"),
    fields=[_field("loan_id"), _field("amount")],
)
CUSTOMER = SemanticDataset(
    name="customer", source="customer", entity=SemanticEntity(),
    fields=[_field("customer_id"), _field("region")],
)
LEDGER = SemanticDataset(
    name="ledger", source="ledger",
    fields=[_field("ledger_id"), _field("amount")],
)


# ── 唯一精确命中 ─────────────────────────────────────────


def test_unique_hit_builds_mapping():
    model = _model(LOAN, CUSTOMER)
    assert build_object_columns(model, ["amount", "region"]) == {
        "amount": {"entity": "贷款", "field": "amount"},
        # display_name 缺省 → 数据集名(D1 读侧回退)
        "region": {"entity": "customer", "field": "region"},
    }


def test_display_name_fallback_same_as_object_graph():
    """FR-GRAPH-5 与 §4.5 给的是同一个名字:两个消费面共用 D1 回退。"""
    model = _model(LOAN, CUSTOMER)
    graph_names = {o["dataset"]: o["display_name"] for o in build_object_graph(model)["objects"]}
    mapping = build_object_columns(model, ["amount", "region"])
    # 声明了显示名 → 用显示名;缺省 → 数据集名;两处逐字相同。
    assert mapping["amount"]["entity"] == graph_names["loan"] == "贷款"
    assert mapping["region"]["entity"] == graph_names["customer"] == "customer"


def test_undeclared_dataset_field_not_mapped():
    """未声明实体的数据集字段不参与 —— 「对象列」描述的是对象字段。"""
    model = _model(LOAN, LEDGER)  # 两边都有 amount,但 ledger 未声明
    assert build_object_columns(model, ["amount"]) == {
        "amount": {"entity": "贷款", "field": "amount"},
    }
    # 只有未声明数据集含有该字段 → 零命中 → 缺席。
    assert build_object_columns(model, ["ledger_id"]) == {}


# ── 撞车/歧义 → 缺席(宁缺不猜)──────────────────────────


def test_two_entities_collide_on_same_field_name():
    """多实体同名字段 → 整列不建映射;其余唯一命中的列照常。"""
    a = SemanticDataset(
        name="a", source="a", entity=SemanticEntity(display_name="甲"),
        fields=[_field("name"), _field("only_a")],
    )
    b = SemanticDataset(
        name="b", source="b", entity=SemanticEntity(display_name="乙"),
        fields=[_field("name")],
    )
    model = _model(a, b)
    got = build_object_columns(model, ["name", "only_a"])
    assert "name" not in got  # 撞车 → 缺席(不是猜一个)
    assert got == {"only_a": {"entity": "甲", "field": "only_a"}}


def test_duplicate_field_declaration_is_conservatively_absent():
    """同一数据集重复声明同名字段(坏模型)→ 命中有两条 → 缺席。"""
    dup = SemanticDataset(
        name="dup", source="dup", entity=SemanticEntity(display_name="重复"),
        fields=[_field("code"), _field("code")],
    )
    assert build_object_columns(_model(dup), ["code"]) == {}


# ── 边界与确定性 ─────────────────────────────────────────


def test_none_model_empty_columns_and_non_str_skipped():
    assert build_object_columns(None, ["amount"]) == {}
    assert build_object_columns(_model(LOAN), []) == {}
    assert build_object_columns(_model(LOAN), None) == {}
    # 非 str / 空串列跳过(精确匹配只认非空字符串列名)。
    assert build_object_columns(_model(LOAN), [123, "", None]) == {}


def test_column_order_preserved_and_input_untouched():
    model = _model(LOAN, CUSTOMER)
    columns = ["region", "amount"]
    before = list(columns)
    got = build_object_columns(model, columns)
    assert list(got) == ["region", "amount"]  # 键序 = 入参列序(不排序)
    assert columns == before  # 输入不被改动
