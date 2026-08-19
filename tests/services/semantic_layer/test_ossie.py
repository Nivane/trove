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
