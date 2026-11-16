"""Agent-facing semantic context (`list_objects` / `describe_object` projections).

钉住四件事:

1. **投影 ≡ 声明**:对象/链接/层级与 ``build_object_graph`` 同源,字段与指标
   按声明顺序展开,可选键缺席时不填默认值;
2. **对象解析不猜**:数据集名 / display_name / 别名 / 数据集 synonyms 都可达,
   多命中 → ``ObjectAmbiguous``(给候选),零命中 → ``ObjectNotFound``(给清单);
3. **对象详情是对象视角**:入边出边分开、锚定指标只取本对象、主题域带生效
   作用域与过期状态;
4. **确定性**:纯函数,同输入逐字节同输出;无模型 → 空骨架不抛。
"""
from __future__ import annotations

import pytest

from trove.services.semantic_layer.agent_context import (
    ObjectAmbiguous,
    ObjectNotFound,
    build_model_overview,
    build_object_detail,
    resolve_object,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticEntity,
    SemanticModel,
)
from trove.services.semantic_layer.object_graph import build_object_graph
from trove.services.semantic_layer.ossie import parse_ossie

MODEL_YAML = """
version: "0.2.0.dev0"
name: demo
description: Demo model for agent context.
ai_context:
  instructions: Prefer customer.region for regional rollups.
datasets:
  - name: loan
    source: loan
    description: One row per loan.
    primary_key: [loan_id]
    unique_keys: [[loan_id, region]]
    row_filter: "status = 'active'"
    ai_context:
      synonyms: [loans_table]
    entity:
      display_name: 贷款
      natural_keys: [loan_id]
      aliases: [借款]
    fields:
      - name: loan_id
        datatype: Integer
        semantic_role: identifier
        expression: {dialects: [{dialect: ANSI_SQL, expression: loan_id}]}
      - name: amount
        datatype: Decimal
        semantic_role: measure
        description: Loan amount
        expression: {dialects: [{dialect: ANSI_SQL, expression: amount}]}
      - name: region
        datatype: String
        semantic_role: enum
        values: [A, B]
        enum_display: {A: 甲, B: 乙}
        ai_context:
          synonyms: [area]
          value_aliases: {A: [alpha], B: [beta]}
        expression: {dialects: [{dialect: ANSI_SQL, expression: region}]}
      - name: full_name
        datatype: String
        expression: {dialects: [{dialect: ANSI_SQL, expression: "CONCAT(first, last)"}]}
      - name: ssn
        datatype: String
        mask: hash
        expression: {dialects: [{dialect: ANSI_SQL, expression: ssn}]}
  - name: customer
    source: customer
    primary_key: [customer_id]
    fields:
      - name: customer_id
        datatype: Integer
        expression: {dialects: [{dialect: ANSI_SQL, expression: customer_id}]}
      - name: customer_region
        datatype: String
        expression: {dialects: [{dialect: ANSI_SQL, expression: customer_region}]}
relationships:
  - name: loan_to_customer
    from: loan
    to: customer
    from_columns: [customer_id]
    to_columns: [customer_id]
    display_name: 属于
  - name: customer_to_loan
    from: customer
    to: loan
    from_columns: [customer_id]
    to_columns: [customer_id]
metrics:
  - name: total_amount
    expression: {dialects: [{dialect: ANSI_SQL, expression: SUM(loan.amount)}]}
    ai_context:
      synonyms: [总额]
      examples: ["total amount?"]
  - name: customer_count
    expression: {dialects: [{dialect: ANSI_SQL, expression: COUNT(customer.customer_id)}]}
hierarchies:
  - name: region_hierarchy
    display_name: 地区层级
    dataset: loan
    levels: [region, loan_id]
topics:
  - name: loans
    description: 贷款主题
    synonyms: [loan]
    datasets: [loan, customer, gone_table]
    metrics: [total_amount]
    examples: [每个地区的贷款金额是多少]
  - name: expired
    datasets: [gone_table]
time_spine:
  field: loan.date
  granularity: month
  fill: "0"
"""


@pytest.fixture()
def model():
    return parse_ossie(MODEL_YAML, preferred_dialect="sqlite")


# ── 对象解析 ────────────────────────────────────────────────────


def test_resolve_by_dataset_name_display_name_alias_and_synonym(model):
    for ref in ("loan", "LOAN", "  Loan  ", "贷款", "借款", "loans_table"):
        assert resolve_object(model, ref).name == "loan"


def test_unknown_object_lists_available(model):
    with pytest.raises(ObjectNotFound) as e:
        resolve_object(model, "nope")
    assert e.value.available == ["loan", "customer"]
    assert "nope" in str(e.value)


def test_ambiguous_reference_never_guesses():
    """同一个别名指向两个数据集 = 歧义,不是「取第一个」。"""
    ambiguous = SemanticModel(datasets=[
        SemanticDataset(name="a", entity=SemanticEntity(aliases=["shared"])),
        SemanticDataset(name="b", entity=SemanticEntity(aliases=["shared"])),
    ])
    with pytest.raises(ObjectAmbiguous) as e:
        resolve_object(ambiguous, "shared")
    assert e.value.candidates == ["a", "b"]


# ── 模型总览 ────────────────────────────────────────────────────


def test_overview_reuses_object_graph(model):
    overview = build_model_overview(model)
    graph = build_object_graph(model)
    assert overview["objects"] == [
        {**o,
         **({"description": "One row per loan."} if o["dataset"] == "loan" else {}),
         **({"synonyms": ["loans_table"]} if o["dataset"] == "loan" else {})}
        for o in graph["objects"]
    ]
    assert overview["links"] == graph["links"]
    assert overview["hierarchies"] == graph["hierarchies"]


def test_overview_metrics_are_summaries_not_expressions(model):
    overview = build_model_overview(model)
    total = next(m for m in overview["metrics"] if m["name"] == "total_amount")
    assert total["datasets"] == ["loan"]
    assert "expression" not in total
    assert "filter" not in total
    assert total["synonyms"] == ["总额"]


def test_overview_topics_carry_effective_scope_and_status(model):
    overview = build_model_overview(model)
    topics = {t["name"]: t for t in overview["topics"]}
    assert topics["loans"]["status"] == "ok"
    # 声明里有 gone_table,生效作用域只保留模型里仍存在的两个数据集。
    assert topics["loans"]["scope"] == ["loan", "customer"]
    assert topics["expired"]["status"] == "empty_scope"
    assert topics["expired"]["scope"] == []


def test_overview_includes_model_metadata_and_time_spine(model):
    overview = build_model_overview(model)
    assert overview["name"] == "demo"
    assert overview["version"] == "0.2.0.dev0"
    assert overview["instructions"].startswith("Prefer customer.region")
    assert overview["time_spine"] == {
        "field": "loan.date", "granularity": "month", "fill": "0",
    }


def test_overview_without_model_is_an_empty_skeleton():
    assert build_model_overview(None) == {
        "objects": [], "links": [], "hierarchies": [],
        "metrics": [], "topics": [],
    }


def test_overview_is_deterministic(model):
    assert build_model_overview(model) == build_model_overview(model)


# ── 对象详情 ────────────────────────────────────────────────────


def test_detail_fields_omit_identity_expression_and_keep_declarations(model):
    detail = build_object_detail(model, resolve_object(model, "loan"))
    fields = {f["name"]: f for f in detail["fields"]}
    assert list(fields) == ["loan_id", "amount", "region", "full_name", "ssn"]

    # 表达式 = 列名本身 → 不重复输出;函数表达式 → 保留
    assert "expression" not in fields["loan_id"]
    assert fields["full_name"]["expression"] == "CONCAT(first, last)"

    assert fields["loan_id"]["role"] == "identifier"
    assert fields["amount"]["description"] == "Loan amount"
    assert fields["region"]["values"] == ["A", "B"]
    assert fields["region"]["enum_display"] == {"A": "甲", "B": "乙"}
    assert fields["region"]["value_aliases"] == {"A": ["alpha"], "B": ["beta"]}
    assert fields["region"]["synonyms"] == ["area"]
    assert fields["ssn"]["mask"] == "hash"


def test_detail_has_only_metrics_anchored_to_the_object(model):
    detail = build_object_detail(model, resolve_object(model, "loan"))
    assert [m["name"] for m in detail["metrics"]] == ["total_amount"]
    assert detail["metrics"][0]["expression"] == "SUM(loan.amount)"
    assert detail["metrics"][0]["datasets"] == ["loan"]


def test_detail_links_are_directional(model):
    detail = build_object_detail(model, resolve_object(model, "loan"))
    by_name = {link["name"]: link for link in detail["links"]}
    assert by_name["loan_to_customer"]["direction"] == "outgoing"
    assert by_name["customer_to_loan"]["direction"] == "incoming"
    # 链接沿用对象图的展示回退(display_name 声明优先)
    assert by_name["loan_to_customer"]["display_name"] == "属于"


def test_detail_keeps_keys_row_filter_and_attachments(model):
    detail = build_object_detail(model, resolve_object(model, "loan"))
    assert detail["dataset"] == "loan"
    assert detail["display_name"] == "贷款"
    assert detail["primary_key"] == ["loan_id"]
    assert detail["unique_keys"] == [["loan_id", "region"]]
    assert detail["row_filter"] == "status = 'active'"
    assert [h["name"] for h in detail["hierarchies"]] == ["region_hierarchy"]
    assert [t["name"] for t in detail["topics"]] == ["loans"]
    assert detail["topics"][0]["examples"] == ["每个地区的贷款金额是多少"]
