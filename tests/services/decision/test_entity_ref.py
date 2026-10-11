"""``entity_ref`` 键位解析单测(本体形态 E8,设计 §5.6)。

纯函数这层钉三件事,都不需要数据库:

  - **与对象图同一份定义**(反漂移):``declared`` 只看 entity 块在不在;
    显示名空 → 数据集名、自然键空 → 主键(D1 回退),与投影逐字一致;
  - **与编译器同一把尺**:dim 引用走 ``query._resolve_field`` —— 裸列名
    跨数据集不唯一就拒绝,不猜;
  - **宁缺不猜**:未声明 / 非键字段 / 坏引用 / 无语义模型 → ``None``,
    解析器(``make_entity_lookup``)任何失败都不外抛。
"""

from __future__ import annotations

import pytest

from trove.services.decision.entity_ref import (
    entity_key_index,
    entity_spec_for,
    make_entity_lookup,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticEntity,
    SemanticField,
    SemanticModel,
)
from trove.services.decision.service import DecisionError


def _field(name: str) -> SemanticField:
    return SemanticField(name=name, expression=name)


def _model() -> SemanticModel:
    """三个数据集,覆盖三种声明形态(与 loader 归一后的形状一致)。

    · ``loan``:全声明(display_name + natural_keys);
    · ``customer``:只声明了 aliases —— 显示名/自然键走 D1 回退
      (loader 对三键全空的块归一为 None,所以「全回退」的真实形状
      就是「只写了别的键」);
    · ``account``:未声明(entity 缺省 = None,主键只是物理约束)。
    """
    return SemanticModel(datasets=[
        SemanticDataset(
            name="loan", source="loan", primary_key=["loan_id"],
            fields=[_field("loan_id"), _field("region"), _field("status")],
            entity=SemanticEntity(display_name="贷款",
                                  natural_keys=["loan_id"]),
        ),
        SemanticDataset(
            name="customer", source="customer", primary_key=["customer_id"],
            fields=[_field("customer_id"), _field("region")],
            entity=SemanticEntity(aliases=["客户"]),
        ),
        SemanticDataset(
            name="account", source="account", primary_key=["account_id"],
            fields=[_field("account_id")],
        ),
    ])


# ── entity_key_index ────────────────────────────────────

def test_declared_entity_indexes_the_canonical_key():
    index = entity_key_index(_model())
    assert index["loan.loan_id"] == {"type": "贷款", "field": "loan_id"}


def test_display_name_falls_back_to_the_dataset_name():
    """D1 读侧回退与对象图页同一份定义:显示名空 → 数据集名。"""
    index = entity_key_index(_model())
    assert index["customer.customer_id"] == {"type": "customer",
                                             "field": "customer_id"}


def test_empty_natural_keys_fall_back_to_the_primary_key():
    """D1 第二回退:自然键空 → primary_key(声明层保持缺失即缺失)。"""
    index = entity_key_index(_model())
    assert "customer.customer_id" in index
    assert "customer.region" not in index  # 非键字段不进索引


def test_undeclared_dataset_contributes_nothing():
    index = entity_key_index(_model())
    assert all(not k.startswith("account.") for k in index)


def test_every_declared_natural_key_is_indexed():
    model = _model()
    model.datasets[0].entity.natural_keys = ["loan_id", "region"]
    index = entity_key_index(model)
    assert index["loan.region"] == {"type": "贷款", "field": "region"}
    assert index["loan.loan_id"]["field"] == "loan_id"


def test_index_is_empty_for_a_missing_model():
    assert entity_key_index(None) == {}


def test_index_is_deterministic_across_calls():
    assert entity_key_index(_model()) == entity_key_index(_model())


# ── entity_spec_for ─────────────────────────────────────

def test_canonical_ref_resolves_to_the_spec():
    assert entity_spec_for(_model(), "loan.loan_id") == {
        "type": "贷款", "field": "loan_id"}


def test_bare_unique_ref_resolves_to_the_spec():
    """``loan_id`` 只在一个数据集里有 —— 裸名可解,与编译器同判定。"""
    assert entity_spec_for(_model(), "loan_id") == {
        "type": "贷款", "field": "loan_id"}


def test_bare_ref_to_an_undeclared_datasets_field_is_absent():
    """裸名解析得动,但落点数据集未声明 —— 不是对象,不是键。"""
    assert entity_spec_for(_model(), "account_id") is None


def test_bare_ref_ambiguous_across_datasets_is_refused():
    """``region`` 在 loan / customer 都有 —— 裸名歧义,解析不了 = 缺席
    (编译器对同一个引用也是拒绝:一把尺)。"""
    assert entity_spec_for(_model(), "region") is None


def test_non_key_field_of_a_declared_entity_is_not_an_entity_key():
    assert entity_spec_for(_model(), "loan.status") is None
    assert entity_spec_for(_model(), "loan.region") is None


def test_unknown_ref_and_empty_ref_are_absent():
    assert entity_spec_for(_model(), "loan.ghost") is None
    assert entity_spec_for(_model(), "ghost.loan_id") is None
    assert entity_spec_for(_model(), "") is None
    assert entity_spec_for(_model(), None) is None


def test_resolution_is_case_insensitive_like_the_compiler():
    assert entity_spec_for(_model(), "LOAN.LOAN_ID") == {
        "type": "贷款", "field": "loan_id"}


def test_missing_model_is_absent_not_an_error():
    assert entity_spec_for(None, "loan.loan_id") is None


def test_returned_spec_is_a_copy():
    first = entity_spec_for(_model(), "loan.loan_id")
    first["type"] = "改坏了"
    assert entity_spec_for(_model(), "loan.loan_id")["type"] == "贷款"


# ── make_entity_lookup(包外执行面) ──────────────────────

class _FakeDecision:
    def __init__(self, *, model=None, dialect_error=None):
        self._model = model
        self._dialect_error = dialect_error

    async def _dialect(self, datasource):
        if self._dialect_error is not None:
            raise self._dialect_error
        return "sqlite"

    def _model_for(self, datasource, dialect):
        if self._model is None:
            raise DecisionError(f"no semantic model for {datasource!r}")
        return self._model


async def test_lookup_returns_the_spec_for_a_declared_key():
    lookup = make_entity_lookup(_FakeDecision(model=_model()))
    assert await lookup("demo", "loan.loan_id") == {
        "type": "贷款", "field": "loan_id"}


async def test_lookup_returns_none_for_an_undeclared_field():
    lookup = make_entity_lookup(_FakeDecision(model=_model()))
    assert await lookup("demo", "loan.status") is None


async def test_lookup_swallows_unreachable_datasource():
    """数据源够不到 → None(引用是展示元数据,缺席是合法结局,绝不外抛)。"""
    lookup = make_entity_lookup(_FakeDecision(
        dialect_error=DecisionError("datasource unreachable")))
    assert await lookup("ghost", "loan.loan_id") is None


async def test_lookup_swallows_a_missing_model():
    lookup = make_entity_lookup(_FakeDecision(model=None))
    assert await lookup("demo", "loan.loan_id") is None


async def test_lookup_never_raises_even_on_contract_breaks():
    class Boom:
        async def _dialect(self, datasource):
            raise RuntimeError("boom")

    lookup = make_entity_lookup(Boom())
    assert await lookup("demo", "loan.loan_id") is None


@pytest.mark.parametrize("ref", ["", "  ", "loan.", ".loan_id"])
async def test_lookup_on_malformed_refs_is_absent(ref):
    lookup = make_entity_lookup(_FakeDecision(model=_model()))
    assert await lookup("demo", ref) is None
