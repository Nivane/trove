"""OSSIE YAML parser tests (Apache Ossie core spec subset).

The parser turns an Ossie semantic model into SemanticModel/SemanticMetric,
picking the dialect expression that matches the active adapter and
extracting the datasets referenced by each metric expression.
"""
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
