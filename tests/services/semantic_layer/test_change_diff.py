"""模型级 diff —— 反射驱动的实体比对（R5 防漏改）+ _draft_diff 适配。"""
from __future__ import annotations

import dataclasses

import pytest

from trove.services.semantic_layer import models as sem_models
from trove.services.semantic_layer.diff import _entity_rows, build_change_diff

RICH_YAML = """version: "0.2.0.dev0"
name: demo
datasets:
- name: loan
  source: loan
  primary_key: [loan_id]
  entity:
    display_name: 贷款
    natural_keys: [loan_id]
    aliases: [借款]
  fields:
  - name: amount
    datatype: Real
    expression:
      dialects:
      - {dialect: ANSI_SQL, expression: amount}
  - name: region
    datatype: String
    expression:
      dialects:
      - {dialect: ANSI_SQL, expression: region}
- name: customer
  source: customer
  primary_key: [customer_id]
  fields:
  - name: customer_id
    datatype: Integer
    expression:
      dialects:
      - {dialect: ANSI_SQL, expression: customer_id}
relationships:
- name: loan_to_customer
  from: loan
  to: customer
  from_columns: [customer_id]
  to_columns: [customer_id]
  display_name: 属于
metrics:
- name: total_loan
  expression:
    dialects:
    - {dialect: ANSI_SQL, expression: SUM(loan.amount)}
topics:
- name: credits
  datasets: [loan]
hierarchies:
- name: product_hierarchy
  display_name: 产品层级
  dataset: loan
  levels: [loan_id, amount]
"""


def _bump(value):
    """把一个字段值扰动成不同的值（按类型逐类处理）。"""
    if isinstance(value, list):
        return [*value, "zzz_probe"]
    if isinstance(value, dict):
        return {**value, "zzz_probe": 1}
    if isinstance(value, str):
        return value + "_probe"
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    if value is None:
        return "probe"
    return "probe"  # 未知类型兜底


def _parse(yaml_text: str):
    from trove.services.semantic_layer.ossie import parse_ossie
    return parse_ossie(yaml_text, preferred_dialect="sqlite")


_SECTIONS = {
    "metrics": ("metrics", sem_models.SemanticMetric),
    "datasets": ("datasets", sem_models.SemanticDataset),
    "fields": ("fields", sem_models.SemanticField),
    "relationships": ("relationships", sem_models.SemanticRelationship),
    "topics": ("topics", sem_models.TopicDomain),
    "hierarchies": ("hierarchies", sem_models.Hierarchy),
}


@pytest.mark.parametrize("section", sorted(_SECTIONS))
def test_entity_rows_cover_all_dataclass_fields(section):
    """R5：比对字典必须覆盖 dataclass 的**全部字段名**——漏一个字段的比对
    就是一个看不见的漏改。fields 节以「dataset.field」为实体名。"""
    _, cls = _SECTIONS[section]
    rows = _entity_rows(_parse(RICH_YAML))[section]
    assert rows, f"{section} 未产出实体（fixture 与解析器漂移？）"
    name, stored = next(iter(rows.items()))
    expected = {f.name for f in dataclasses.fields(cls)}
    if section == "datasets":
        expected -= {"fields"}  # 数据集层只比字段名单,细节在 fields 节
        assert set(stored) >= expected | {"fields"}
    else:
        assert set(stored) >= expected


@pytest.mark.parametrize("section", sorted(_SECTIONS))
def test_every_field_change_is_visible(section):
    """逐字段扰动注入：任一 dataclass 字段变化都必须落进 modified/added。"""
    from trove.services.semantic_layer.diff import _diff_rows

    _, cls = _SECTIONS[section]
    model = _parse(RICH_YAML)
    target = (model.datasets[0].fields[0] if section == "fields"
              else getattr(model, section)[0])
    for f in dataclasses.fields(cls):
        if section == "datasets" and f.name == "fields":
            continue  # 字段名单的细节由 fields 节承担（下面另有专测）
        changed = dataclasses.replace(
            target, **{f.name: _bump(getattr(target, f.name))})
        if section == "fields":
            ds = dataclasses.replace(
                model.datasets[0],
                fields=[changed, *model.datasets[0].fields[1:]])
            after = dataclasses.replace(model, datasets=[ds, *model.datasets[1:]])
        else:
            after = dataclasses.replace(
                model, **{section: [changed, *getattr(model, section)[1:]]})
        diff = _diff_rows(_entity_rows(model), _entity_rows(after), [])
        assert diff.entities[section].modified or diff.entities[section].added, \
            f"{section}.{f.name} 的变化不可见"


def test_entity_declaration_change_produces_a_diff_row():
    """改 entity(对象视图声明)必须在变更历史里可见。

    entity 是 dataset 的字段,经反射进 datasets 行的比对 —— 这正是
    「E1 三块声明在 diff 里成活」的第一块。
    """
    from trove.services.semantic_layer.diff import _diff_rows

    model = _parse(RICH_YAML)
    entity = dataclasses.replace(model.datasets[0].entity, display_name="贷款对象")
    ds = dataclasses.replace(model.datasets[0], entity=entity)
    after = dataclasses.replace(model, datasets=[ds, *model.datasets[1:]])
    diff = _diff_rows(_entity_rows(model), _entity_rows(after), [])
    assert diff.entities["datasets"].modified == ["loan"]


def test_hierarchy_added_and_removed_are_visible():
    from trove.services.semantic_layer.diff import _diff_rows

    model = _parse(RICH_YAML)
    dropped = dataclasses.replace(model, hierarchies=[])
    diff = _diff_rows(_entity_rows(model), _entity_rows(dropped), [])
    assert diff.entities["hierarchies"].removed == ["product_hierarchy"]
    diff = _diff_rows(_entity_rows(dropped), _entity_rows(model), [])
    assert diff.entities["hierarchies"].added == ["product_hierarchy"]


def test_relationship_display_name_change_is_visible():
    from trove.services.semantic_layer.diff import _diff_rows

    model = _parse(RICH_YAML)
    rel = dataclasses.replace(model.relationships[0], display_name="归属")
    after = dataclasses.replace(model, relationships=[rel, *model.relationships[1:]])
    diff = _diff_rows(_entity_rows(model), _entity_rows(after), [])
    assert diff.entities["relationships"].modified == ["loan_to_customer"]


def test_dataset_row_compares_field_names_only():
    """datasets 行以字段名单参与比对（字段级细节归 fields 节，不重复计数）。"""
    from trove.services.semantic_layer.diff import _diff_rows

    model = _parse(RICH_YAML)
    ds = dataclasses.replace(model.datasets[0], fields=model.datasets[0].fields[:1])
    after = dataclasses.replace(model, datasets=[ds, *model.datasets[1:]])
    diff = _diff_rows(_entity_rows(model), _entity_rows(after), [])
    assert diff.entities["datasets"].modified == ["loan"]
    assert diff.entities["fields"].removed == ["loan.region"]


def test_build_change_diff_adapts_draft_diff():
    """details = 逐 payload 的 _draft_diff 原样透传（带 carryover 语义）。"""
    base = {"version": "0.2.0.dev0",
"name": "demo", "datasets": [], "metrics": [
                {"name": "total_loan", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "SUM(loan.amount)"}]}}]}
    payloads = [{"kind": "metric", "action": "upsert", "name": "total_loan",
                 "payload": {"expression": "SUM(loan.amount) - SUM(loan.refund)"}}]
    after = {"version": "0.2.0.dev0",
"name": "demo", "datasets": [], "metrics": [
                 {"name": "total_loan", "expression": {"dialects": [
                     {"dialect": "ANSI_SQL", "expression": "SUM(loan.amount) - SUM(loan.refund)"}]}}]}
    diff = build_change_diff(base, after, payloads, dialect="sqlite")
    assert diff.entities["metrics"].modified == ["total_loan"]
    assert diff.details and diff.details[0]["name"] == "total_loan"
    assert any(r["changed"] for r in diff.details[0]["fields"])
    assert diff.to_dict()["entities"]["metrics"]["modified"] == ["total_loan"]
    assert diff.from_dict(diff.to_dict()).details == diff.details


def test_added_and_removed():
    base = {"version": "0.2.0.dev0",
"name": "demo", "datasets": [],
            "metrics": [{"name": "a", "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": "SUM(t.x)"}]}}]}
    after = {"version": "0.2.0.dev0",
"name": "demo", "datasets": [],
             "metrics": [{"name": "b", "expression": {"dialects": [
                 {"dialect": "ANSI_SQL", "expression": "SUM(t.y)"}]}}]}
    diff = build_change_diff(base, after, [])
    assert diff.entities["metrics"].added == ["b"]
    assert diff.entities["metrics"].removed == ["a"]


def test_unparseable_docs_degrade_to_details_only():
    """坏文档不抛：实体层退化为空比对（diff 是评审辅助,不是门禁）。"""
    bad = {"version": "0.2.0.dev0", "metrics": [{"name": "m"}]}  # 无 expression
    diff = build_change_diff(bad, None, [])
    assert diff.entities == {}
    assert diff.details == []
