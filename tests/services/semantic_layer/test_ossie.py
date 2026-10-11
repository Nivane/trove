"""OSSIE YAML parser tests (Apache Ossie core spec subset).

The parser turns an Ossie semantic model into SemanticModel/SemanticMetric,
picking the dialect expression that matches the active adapter and
extracting the datasets referenced by each metric expression.
"""
import logging

import pytest

from trove.services.semantic_layer.ossie import parse_ossie

SAMPLE = """
semantic_model:
  - name: financial_analytics
    description: Financial demo analytics model
    ai_context:
      instructions: "Use this model for banking and loan analysis"
    datasets:
      - name: loan
        source: financial.loan
        description: Loan records
      - name: account
        source: financial.account
        description: Bank accounts
    metrics:
      - name: total_loan_amount
        description: Total amount of all loans
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(loan.amount)
        ai_context:
          synonyms:
            - "total loans"
            - "loan volume"
      - name: avg_loan_per_account
        description: Average loan amount per account
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(loan.amount) / COUNT(DISTINCT account.account_id)
"""


def test_parse_extracts_model_instructions():
    model = parse_ossie(SAMPLE, preferred_dialect="sqlite")

    assert model.name == "financial_analytics"
    assert model.description == "Financial demo analytics model"
    assert model.instructions == "Use this model for banking and loan analysis"


def test_parse_extracts_metrics_with_synonyms():
    model = parse_ossie(SAMPLE, preferred_dialect="sqlite")

    assert [m.name for m in model.metrics] == [
        "total_loan_amount", "avg_loan_per_account"]
    metric = model.metrics[0]
    assert metric.expression == "SUM(loan.amount)"
    assert metric.synonyms == ["total loans", "loan volume"]
    assert metric.definition == "Total amount of all loans"


def test_parse_metric_filter_agg_time_non_additive():
    """metric 级 filter / agg_time_dimension / non_additive 解析。"""
    yaml_text = """
semantic_model:
  - name: fin
    datasets:
      - name: loan
        fields:
          - name: loan_id
          - name: status
          - name: date
    metrics:
      - name: active_loan_count
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: COUNT(loan.loan_id)
        filter: "status = 'A'"
        agg_time_dimension: loan.date
        non_additive: false
      - name: distinct_loans
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: COUNT(DISTINCT loan.loan_id)
        non_additive: true
"""
    model = parse_ossie(yaml_text, preferred_dialect="sqlite")
    active, distinct_ = model.metrics
    assert active.filter == "status = 'A'"
    assert active.agg_time_dimension == "loan.date"
    assert active.non_additive is False
    assert distinct_.filter == ""
    assert distinct_.non_additive is True


def test_datasets_extracted_from_expression():
    model = parse_ossie(SAMPLE, preferred_dialect="sqlite")

    assert model.metrics[0].datasets == ["loan"]
    # 跨数据集 metric:两个数据集的引用都提取到
    assert model.metrics[1].datasets == ["account", "loan"]


def test_bare_column_metric_is_table_agnostic():
    """表达式没有 数据集.字段 限定 → datasets 为空(模型级渲染,不做表锚定)。"""
    yaml_text = SAMPLE.replace(
        "SUM(loan.amount) / COUNT(DISTINCT account.account_id)",
        "SUM(amount)",
    )
    model = parse_ossie(yaml_text, preferred_dialect="sqlite")

    assert model.metrics[1].datasets == []


def test_dialect_preference_uses_adapter_dialect_then_ansi():
    yaml_text = SAMPLE.replace(
        "              expression: SUM(loan.amount)\n",
        "              expression: SUM(loan.amount)\n"
        "            - dialect: SNOWFLAKE\n"
        "              expression: SUM(loan.amount)::NUMBER\n",
    )

    # adapter 方言在声明里 → 用它的表达式
    model = parse_ossie(yaml_text, preferred_dialect="snowflake")
    assert model.metrics[0].expression == "SUM(loan.amount)::NUMBER"

    # adapter 方言不在声明里 → 回退 ANSI_SQL
    model2 = parse_ossie(yaml_text, preferred_dialect="sqlite")
    assert model2.metrics[0].expression == "SUM(loan.amount)"


def test_parse_rejects_metric_without_expression():
    with pytest.raises(ValueError):
        parse_ossie(
            "semantic_model:\n  - name: x\n    metrics:\n      - name: m\n",
            preferred_dialect="sqlite",
        )


FULL_STRUCTURE = """
semantic_model:
  - name: financial
    description: Full structure model
    datasets:
      - name: loan
        source: financial.loan
        primary_key: [loan_id]
        fields:
          - name: status
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: status
            datatype: String
            description: loan repayment status
            ai_context:
              synonyms: [repayment state]
          - name: issue_date
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: issue_date
            datatype: Date
      - name: account
        source: financial.account
        primary_key: [account_id]
        fields:
          - name: district_id
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: district_id
            datatype: Integer
            dimension:
              is_time: false
          - name: created_at
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: created_at
            datatype: DateTime
            dimension:
              is_time: false
    relationships:
      - name: loan_to_account
        from: loan
        to: account
        from_columns: [account_id]
        to_columns: [account_id]
    metrics:
      - name: total_loan_amount
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(loan.amount)
"""


def test_parse_full_structure_datasets_and_keys():
    model = parse_ossie(FULL_STRUCTURE, preferred_dialect="sqlite")

    assert [d.name for d in model.datasets] == ["loan", "account"]
    assert model.datasets[0].source == "financial.loan"
    assert model.datasets[0].primary_key == ["loan_id"]
    assert model.datasets[1].primary_key == ["account_id"]


def test_parse_fields_with_datatype_and_synonyms():
    model = parse_ossie(FULL_STRUCTURE, preferred_dialect="sqlite")

    fields = model.datasets[0].fields
    assert [f.name for f in fields] == ["status", "issue_date"]
    assert fields[0].expression == "status"
    assert fields[0].datatype == "String"
    assert fields[0].synonyms == ["repayment state"]
    assert fields[0].is_time is False  # String → 非时态


def test_is_time_defaults_from_temporal_datatype():
    model = parse_ossie(FULL_STRUCTURE, preferred_dialect="sqlite")
    loan_fields = model.datasets[0].fields

    # 未显式声明 is_time,datatype=Date → 默认时态
    assert loan_fields[1].is_time is True

    account = model.datasets[1]
    # 显式 is_time: false 覆盖时态默认
    assert account.fields[0].is_time is False
    assert account.fields[1].is_time is False


def test_parse_relationships():
    model = parse_ossie(FULL_STRUCTURE, preferred_dialect="sqlite")

    assert len(model.relationships) == 1
    rel = model.relationships[0]
    assert rel.name == "loan_to_account"
    assert rel.from_ == "loan"
    assert rel.to == "account"
    assert rel.from_columns == ["account_id"]
    assert rel.to_columns == ["account_id"]


def test_parse_relationship_cardinality():
    m2n = FULL_STRUCTURE.replace(
        "      - name: loan_to_account\n        from: loan\n        to: account",
        "      - name: loan_to_account\n        from: loan\n        to: account\n"
        "        cardinality: M:N",
    )
    model = parse_ossie(m2n, preferred_dialect="sqlite")
    assert model.relationships[0].cardinality == "M:N"

    # 未声明 → 空(消费端按安全 many→one 处理)
    model = parse_ossie(FULL_STRUCTURE, preferred_dialect="sqlite")
    assert model.relationships[0].cardinality == ""


def test_parse_relationship_fan_out():
    m2n = FULL_STRUCTURE.replace(
        "      - name: loan_to_account\n        from: loan\n        to: account",
        "      - name: loan_to_account\n        from: loan\n        to: account\n"
        "        cardinality: M:N\n        fan_out: dedup",
    )
    model = parse_ossie(m2n, preferred_dialect="sqlite")
    assert model.relationships[0].fan_out == "dedup"

    model = parse_ossie(FULL_STRUCTURE, preferred_dialect="sqlite")
    assert model.relationships[0].fan_out == ""


def test_parse_time_spine():
    with_spine = FULL_STRUCTURE.replace(
        "    metrics:",
        "    time_spine:\n"
        "      field: loan.date\n"
        "      granularity: month\n"
        "      fill: '0'\n"
        "    metrics:",
    )
    model = parse_ossie(with_spine, preferred_dialect="sqlite")
    assert model.time_spine is not None
    assert model.time_spine.field == "loan.date"
    assert model.time_spine.granularity == "month"
    assert model.time_spine.fill == "0"

    model = parse_ossie(FULL_STRUCTURE, preferred_dialect="sqlite")
    assert model.time_spine is None


def test_parse_time_spine_invalid_grain_defaults():
    with_bad = FULL_STRUCTURE.replace(
        "    metrics:",
        "    time_spine:\n"
        "      field: loan.date\n"
        "      granularity: decade\n"
        "    metrics:",
    )
    model = parse_ossie(with_bad, preferred_dialect="sqlite")
    assert model.time_spine is not None
    assert model.time_spine.granularity == "month"


def test_parse_relationship_with_unknown_endpoint_dropped():
    bad = FULL_STRUCTURE.replace(
        "        from: loan\n        to: account",
        "        from: loan\n        to: ghosts",
    )
    model = parse_ossie(bad, preferred_dialect="sqlite")
    assert model.relationships == []


def test_parse_relationship_key_mismatch_dropped():
    bad = FULL_STRUCTURE.replace(
        "        from_columns: [account_id]\n        to_columns: [account_id]",
        "        from_columns: [account_id, x]\n        to_columns: [account_id]",
    )
    model = parse_ossie(bad, preferred_dialect="sqlite")
    assert model.relationships == []


def test_parse_field_without_expression_dropped_lenient():
    no_expr = (
        "          - name: issue_date\n"
        "            expression:\n"
        "              dialects:\n"
        "                - dialect: ANSI_SQL\n"
        "                  expression: issue_date\n"
    )
    bad = FULL_STRUCTURE.replace(no_expr, "          - name: issue_date\n            datatype: Date\n")
    model = parse_ossie(bad, preferred_dialect="sqlite")
    assert [f.name for f in model.datasets[0].fields] == ["status"]


def test_parse_backwards_compatible_without_fields():
    """无 fields/relationships 的旧格式仍能解析(metrics/datasets 照常)。"""
    model = parse_ossie(SAMPLE, preferred_dialect="sqlite")
    assert model.datasets[0].fields == []
    assert model.datasets[0].primary_key == []
    assert model.relationships == []


# ── OSSIE v0.2.0.dev0 全字段:version / unique_keys / label / metric
#    datatype / examples / custom_extensions ───────────────────────────


FULL_V020 = """
version: 0.2.0.dev0
semantic_model:
  - name: fin
    description: full spec model
    ai_context:
      instructions: "use for banking"
      examples: ["How many loans?", "Total amount by region"]
    custom_extensions:
      - vendor_name: DBT
        data: '{"materialized": "table"}'
    datasets:
      - name: client
        source: financial.client
        primary_key: [client_id]
        unique_keys:
          - [client_id]
          - [email]
        ai_context:
          examples: ["list all clients"]
        custom_extensions:
          - vendor_name: SNOWFLAKE
            data: '{"warehouse": "ANALYTICS_WH"}'
        fields:
          - name: client_id
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: client_id
            datatype: Integer
          - name: gender
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: gender
            datatype: String
            label: demographics
            enum_display: {F: female, M: male}
            ai_context:
              synonyms: [sex]
              examples: ["filter by gender"]
            custom_extensions:
              - vendor_name: COMMON
                data: '{"pii": false}'
    metrics:
      - name: number of clients
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: COUNT(client.client_id)
        datatype: Integer
        ai_context:
          synonyms: [client count]
          examples: ["how many clients do we have"]
        custom_extensions:
          - vendor_name: DBT
            data: '{"model": "client"}'
"""


def test_parse_full_v020_fields():
    model = parse_ossie(FULL_V020, preferred_dialect="sqlite")

    # 文档级 version + 模型级 examples / custom_extensions
    assert model.version == "0.2.0.dev0"
    assert model.examples == ["How many loans?", "Total amount by region"]
    assert model.custom_extensions == [
        {"vendor_name": "DBT", "data": '{"materialized": "table"}'}]

    # dataset unique_keys / examples / custom_extensions
    client = model.datasets[0]
    assert client.unique_keys == [["client_id"], ["email"]]
    assert client.examples == ["list all clients"]
    assert client.custom_extensions[0]["vendor_name"] == "SNOWFLAKE"

    # field label / examples / custom_extensions
    gender = next(f for f in client.fields if f.name == "gender")
    assert gender.label == "demographics"
    assert gender.examples == ["filter by gender"]
    assert gender.custom_extensions == [
        {"vendor_name": "COMMON", "data": '{"pii": false}'}]

    # metric datatype / examples / custom_extensions
    m = model.metrics[0]
    assert m.datatype == "Integer"
    assert m.examples == ["how many clients do we have"]
    assert m.custom_extensions[0]["vendor_name"] == "DBT"


def test_parse_v020_missing_optional_fields_default():
    """缺省的 v0.2.0 扩展面全为默认值(旧文件解析不变)。"""
    model = parse_ossie(SAMPLE, preferred_dialect="sqlite")
    assert model.version == ""
    assert model.examples == []
    assert model.custom_extensions == []
    assert model.datasets[0].unique_keys == []
    assert model.datasets[0].custom_extensions == []
    for f in model.datasets[0].fields:
        assert f.label == ""
        assert f.examples == []
        assert f.custom_extensions == []
    for m in model.metrics:
        assert m.datatype is None
        assert m.examples == []
        assert m.custom_extensions == []


def test_parse_v020_malformed_custom_extensions_dropped():
    """custom_extensions 只保留 {vendor_name, data} 形式;坏条目静默丢弃。"""
    yaml_text = """
semantic_model:
  - name: m
    custom_extensions:
      - vendor_name: OK
        data: x
      - data: missing vendor
      - vendor_name: ""
      - "not a dict"
    metrics: []
"""
    model = parse_ossie(yaml_text, preferred_dialect="sqlite")
    assert model.custom_extensions == [{"vendor_name": "OK", "data": "x"}]


def test_metric_type_parsed():
    yaml_text = """
semantic_model:
  - name: m
    datasets: []
    metrics:
      - name: r1
        type: ratio
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: "a / b"
      - name: r2
        type: DERIVED
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: "a / b"
      - name: r3
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: "a / b"
"""
    model = parse_ossie(yaml_text, preferred_dialect="sqlite")
    by_name = {m.name: m for m in model.metrics}
    assert by_name["r1"].metric_type == "ratio"
    assert by_name["r2"].metric_type == "derived"  # 大小写归一
    assert by_name["r3"].metric_type == ""  # 缺省为空(旧文件解析不变)


def test_parse_enum_display():
    """enum_display 解析(code → 人类可读词)。"""
    yaml_text = """
semantic_model:
  - name: m
    datasets:
      - name: client
        fields:
          - name: gender
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: gender
            semantic_role: enum
            enum_display: {F: female, M: male}
"""
    model = parse_ossie(yaml_text, preferred_dialect="sqlite")
    gender = model.datasets[0].fields[0]
    assert gender.semantic_role == "enum"
    assert gender.enum_display == {"F": "female", "M": "male"}


def test_parse_value_aliases():
    """ai_context.value_aliases 多标签值词典解析(list 与逗号串两种形态)。"""
    yaml_text = """
semantic_model:
  - name: m
    datasets:
      - name: loan
        fields:
          - name: status
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: status
            semantic_role: enum
            enum_display: {A: finished, no problems, C: running, no problems}
            ai_context:
              value_aliases:
                A: ["fully paid", "paid off"]
                C: "still running, in progress"
"""
    model = parse_ossie(yaml_text, preferred_dialect="sqlite")
    status = model.datasets[0].fields[0]
    assert status.value_aliases == {
        "A": ["fully paid", "paid off"],
        "C": ["still running", "in progress"],
    }


def test_parse_dataset_row_filter():
    """声明层 RLS:dataset.row_filter 解析为模型字段。"""
    yaml_text = """
semantic_model:
  - name: fin
    datasets:
      - name: client
        source: client
        primary_key: [client_id]
        row_filter: "region = 'EU'"
        fields:
          - name: client_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: client_id}]}
          - name: region
            expression: {dialects: [{dialect: ANSI_SQL, expression: region}]}
    metrics: []
"""
    model = parse_ossie(yaml_text, preferred_dialect="sqlite")
    assert model.datasets[0].row_filter == "region = 'EU'"


def test_parse_dataset_row_filter_defaults_empty():
    model = parse_ossie("""
semantic_model:
  - name: fin
    datasets:
      - name: client
        fields:
          - name: client_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: client_id}]}
    metrics: []
""", preferred_dialect="sqlite")
    assert model.datasets[0].row_filter == ""


# ── 对象视图声明(entity / relationship.display_name / hierarchies)──

ENTITY_YAML = """
semantic_model:
  - name: fin
    datasets:
      - name: loan
        primary_key: [loan_id]
        entity:
          display_name: 贷款
          natural_keys: [loan_id, contract_no]
          aliases: [借款, loan]
        fields:
          - name: loan_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: loan_id}]}
      - name: account
        primary_key: [account_id]
        fields:
          - name: account_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: account_id}]}
    relationships:
      - name: loan_to_account
        from: loan
        to: account
        from_columns: [account_id]
        to_columns: [account_id]
        display_name: 属于
    hierarchies:
      - name: product_hierarchy
        display_name: 产品层级
        dataset: loan
        levels: [product_class, product_name]
    metrics: []
"""

_ENTITY_BLOCK = (
    "        entity:\n"
    "          display_name: 贷款\n"
    "          natural_keys: [loan_id, contract_no]\n"
    "          aliases: [借款, loan]\n"
)


def test_parse_dataset_entity_sub_block():
    model = parse_ossie(ENTITY_YAML, preferred_dialect="sqlite")
    entity = model.datasets[0].entity
    assert entity is not None
    assert entity.display_name == "贷款"
    assert entity.natural_keys == ["loan_id", "contract_no"]
    assert entity.aliases == ["借款", "loan"]
    # 未声明 entity 的兄弟数据集不受影响
    assert model.datasets[1].entity is None


def test_parse_entity_aliases_are_cleaned():
    """复用 ``_clean_names``:空白丢弃、保序去重、裸标量容忍。"""
    text = """
semantic_model:
  - name: fin
    datasets:
      - name: loan
        entity:
          aliases: [" 借款 ", 借款, "", loan]
        fields: []
    metrics: []
"""
    model = parse_ossie(text, preferred_dialect="sqlite")
    entity = model.datasets[0].entity
    assert entity is not None
    assert entity.aliases == ["借款", "loan"]  # 去空白 + 保序去重
    assert entity.display_name == ""

    scalar = ENTITY_YAML.replace(
        _ENTITY_BLOCK,
        "        entity: {aliases: ' 借款 '}\n")
    assert scalar != ENTITY_YAML  # 替换确实生效(fixture 漂移会红)
    entity = parse_ossie(scalar, preferred_dialect="sqlite").datasets[0].entity
    assert entity is not None
    assert entity.aliases == ["借款"]  # 裸标量 → 单元素,两侧空白去掉


def test_empty_entity_block_is_none():
    """``entity: {}``(三键全空)与缺席同义 → None,不产生「全是默认」的中间态。"""
    stripped = ENTITY_YAML.replace(_ENTITY_BLOCK, "        entity: {}\n")
    assert stripped != ENTITY_YAML  # 替换确实生效(fixture 漂移会红)
    model = parse_ossie(stripped, preferred_dialect="sqlite")
    assert model.datasets[0].entity is None


@pytest.mark.parametrize("block", ["5", "[1, 2]", "a bare string"])
def test_bad_shape_entity_block_is_ignored(block):
    """坏形状忽略而不是猜 —— 且不得把整个模型解析炸掉。"""
    text = ENTITY_YAML.replace(_ENTITY_BLOCK, f"        entity: {block}\n")
    model = parse_ossie(text, preferred_dialect="sqlite")
    assert model.datasets[0].entity is None


def test_entity_unknown_sub_keys_are_ignored():
    """未知子键不报错也不产生字段 —— 只有三键声明被认。"""
    text = ENTITY_YAML.replace(
        "          aliases: [借款, loan]\n",
        "          aliases: [借款, loan]\n          future_key: whatever\n")
    model = parse_ossie(text, preferred_dialect="sqlite")
    entity = model.datasets[0].entity
    assert entity is not None
    assert entity.display_name == "贷款"
    assert not hasattr(entity, "future_key")


def test_parse_relationship_display_name():
    model = parse_ossie(ENTITY_YAML, preferred_dialect="sqlite")
    assert model.relationships[0].display_name == "属于"


def test_parse_relationship_display_name_defaults_empty():
    """未声明 → 空串(读侧回退列连接串,声明层保持缺失即缺失)。"""
    model = parse_ossie(FULL_STRUCTURE, preferred_dialect="sqlite")
    assert model.relationships[0].display_name == ""


def test_parse_hierarchies():
    model = parse_ossie(ENTITY_YAML, preferred_dialect="sqlite")
    assert len(model.hierarchies) == 1
    h = model.hierarchies[0]
    assert h.name == "product_hierarchy"
    assert h.display_name == "产品层级"
    assert h.dataset == "loan"
    assert h.levels == ["product_class", "product_name"]


def test_hierarchy_levels_cleaned_and_deduped():
    text = """
semantic_model:
  - name: fin
    datasets: [{name: loan}]
    hierarchies:
      - name: h
        dataset: loan
        levels: [a, " a ", "", b]
    metrics: []
"""
    model = parse_ossie(text, preferred_dialect="sqlite")
    assert model.hierarchies[0].levels == ["a", "b"]


@pytest.mark.parametrize("levels,expected", [("region", ["region"]), (5, [])])
def test_hierarchy_levels_scalar_tolerance(levels, expected):
    """裸标量 → 单元素(与 ``_clean_names`` 同法);非列表容器 → 空。"""
    text = f"""
semantic_model:
  - name: fin
    datasets: [{{name: loan}}]
    hierarchies:
      - name: h
        dataset: loan
        levels: {levels!r}
    metrics: []
"""
    model = parse_ossie(text, preferred_dialect="sqlite")
    assert model.hierarchies[0].levels == expected


def test_bad_hierarchy_entries_dropped_leniently():
    """非字典条目 / 空名条目丢弃,其余照常解析(与 topics 同款宽容)。"""
    text = """
semantic_model:
  - name: fin
    datasets: [{name: loan}]
    hierarchies:
      - "not a dict"
      - {dataset: loan, levels: [a]}
      - {name: "   ", dataset: loan}
      - {name: good, dataset: loan, levels: [x]}
    metrics: []
"""
    model = parse_ossie(text, preferred_dialect="sqlite")
    assert [h.name for h in model.hierarchies] == ["good"]


def test_absent_object_view_declarations_default_inert():
    """存量模型(无三块声明):entity None、display_name 空串、hierarchies 空。"""
    model = parse_ossie(FULL_STRUCTURE, preferred_dialect="sqlite")
    assert all(d.entity is None for d in model.datasets)
    assert model.hierarchies == []


def test_document_level_entity_is_ignored_with_a_warning(caplog):
    """文档根出现 entity = 放错层:不生效、不崩,但必须出声(N1–N3)。"""
    text = """
entity:
  display_name: 贷款
semantic_model:
  - name: fin
    datasets: [{name: loan}]
    metrics: []
"""
    with caplog.at_level(
        logging.WARNING, logger="trove.services.semantic_layer.ossie"
    ):
        model = parse_ossie(text, preferred_dialect="sqlite")
    assert model.datasets[0].entity is None
    assert any("entity" in r.getMessage() and "datasets" in r.getMessage()
               for r in caplog.records)


def test_document_level_hierarchies_is_ignored_with_a_warning(caplog):
    text = """
hierarchies:
  - name: h
    dataset: loan
    levels: [a]
semantic_model:
  - name: fin
    datasets: [{name: loan}]
    metrics: []
"""
    with caplog.at_level(
        logging.WARNING, logger="trove.services.semantic_layer.ossie"
    ):
        model = parse_ossie(text, preferred_dialect="sqlite")
    assert model.hierarchies == []
    assert any("hierarchies" in r.getMessage()
               and "semantic_model[0]" in r.getMessage()
               for r in caplog.records)


def test_misplaced_declarations_do_not_crash_the_parse():
    """两块同时放错层:出声但模型照常解析出来(provider 不会因此丢 last-good)。"""
    text = """
entity: {display_name: x}
hierarchies: [{name: h, dataset: loan}]
semantic_model:
  - name: fin
    datasets: [{name: loan}]
    metrics: []
"""
    model = parse_ossie(text, preferred_dialect="sqlite")
    assert model.name == "fin"
    assert [d.name for d in model.datasets] == ["loan"]


def test_model_level_entity_is_not_a_dataset_entity():
    """entity 只认 dataset 子块:写进 semantic_model[0] 顶层不会被当声明认领。"""
    text = """
semantic_model:
  - name: fin
    entity: {display_name: 贷款}
    datasets: [{name: loan}]
    metrics: []
"""
    model = parse_ossie(text, preferred_dialect="sqlite")
    assert model.datasets[0].entity is None
