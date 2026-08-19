"""SemanticLayerProvider tests: mtime cache, validation, last-known-good.

The provider reads OSSIE YAML files live from a directory (per-datasource),
re-parsing only when a file's mtime/size changes, validating each metric
expression, and keeping the last known good model when a file breaks.
"""
import pytest

from trove.services.semantic_layer.ossie import parse_ossie
from trove.services.semantic_layer.provider import SemanticLayerProvider

SAMPLE = """
semantic_model:
  - name: financial_analytics
    ai_context:
      instructions: "Use this model for banking and loan analysis"
    datasets:
      - name: loan
        source: financial.loan
      - name: account
        source: financial.account
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

TWO_DATASET_METRICS = """
semantic_model:
  - name: financial_analytics
    datasets:
      - name: loan
        source: financial.loan
      - name: account
        source: financial.account
    metrics:
      - name: metric_on_loan
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(loan.amount)
      - name: metric_on_account
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: COUNT(account.account_id)
"""


@pytest.fixture
def semantic_dir(tmp_path):
    d = tmp_path / "semantic" / "financial"
    d.mkdir(parents=True)
    return d


def _write(semantic_dir, text, name="model.yml"):
    (semantic_dir / name).write_text(text)


def test_disabled_when_directory_missing(tmp_path):
    p = SemanticLayerProvider(tmp_path / "missing", "financial")

    assert p.enabled is False
    assert p.metrics() == []
    assert p.terms_for("total loans", ["loan"]) == []


def test_metrics_returned_from_file(semantic_dir):
    _write(semantic_dir, SAMPLE)
    p = SemanticLayerProvider(semantic_dir, "financial")

    metrics = p.metrics()
    assert [m.name for m in metrics] == ["total_loan_amount", "avg_loan_per_account"]
    assert metrics[0].expression == "SUM(loan.amount)"
    assert metrics[0].synonyms == ["total loans", "loan volume"]


def test_caches_parse_until_file_changes(semantic_dir):
    _write(semantic_dir, SAMPLE)
    calls = []

    def counting_parser(text: str):
        calls.append(text)
        return parse_ossie(text, preferred_dialect="sqlite")

    p = SemanticLayerProvider(semantic_dir, "financial", parser=counting_parser)
    assert len(p.metrics()) == 2
    assert len(p.metrics()) == 2  # 未变 → 命中缓存,不再解析
    assert len(calls) == 1

    _write(semantic_dir, SAMPLE.replace("Total amount of all loans", "Sum of all loans"))
    assert p.metrics()[0].definition == "Sum of all loans"
    assert len(calls) == 2  # 文件变了 → 重新解析


def test_parse_failure_keeps_last_good(semantic_dir, caplog):
    _write(semantic_dir, SAMPLE)
    p = SemanticLayerProvider(semantic_dir, "financial")
    assert len(p.metrics()) == 2

    _write(semantic_dir, "[[[broken yaml")
    with caplog.at_level("WARNING"):
        metrics = p.metrics()
    assert len(metrics) == 2  # 回退 last-known-good
    assert metrics[0].name == "total_loan_amount"
    assert "semantic" in caplog.text.lower()


def test_invalid_expression_dropped(semantic_dir, caplog):
    _write(semantic_dir, SAMPLE.replace("SUM(loan.amount)", "SUM((", 1))
    p = SemanticLayerProvider(semantic_dir, "financial")

    with caplog.at_level("WARNING"):
        metrics = p.metrics()
    assert [m.name for m in metrics] == ["avg_loan_per_account"]
    assert "total_loan_amount" in caplog.text


def test_metric_with_unknown_dataset_dropped(semantic_dir):
    _write(semantic_dir, TWO_DATASET_METRICS)
    p = SemanticLayerProvider(semantic_dir, "financial", table_exists=lambda t: t == "account")

    metrics = p.metrics()
    assert [m.name for m in metrics] == ["metric_on_account"]


def test_terms_for_matches_name_and_synonyms(semantic_dir):
    _write(semantic_dir, SAMPLE)
    p = SemanticLayerProvider(semantic_dir, "financial")

    hits = p.terms_for("What is the total loans volume?", ["loan", "account"])
    assert [h.term for h in hits] == ["total_loan_amount"]
    assert hits[0].mapping == "SUM(loan.amount)"
    assert hits[0].aliases == ["total loans", "loan volume"]
    assert hits[0].tables == ["loan"]

    assert p.terms_for("how many accounts?", ["account"]) == []


def test_terms_for_anchored_to_matched_tables(semantic_dir):
    _write(semantic_dir, SAMPLE)
    p = SemanticLayerProvider(semantic_dir, "financial")

    # 只匹配到 account → 只引用 account 的 metric 保留;avg 跨两表也算
    hits = p.terms_for("avg loan per account", ["account"])
    assert [h.term for h in hits] == ["avg_loan_per_account"]

    # 只匹配到 district(两个 metric 都不引用)→ 全过滤
    assert p.terms_for("avg loan per account", ["district"]) == []


def test_terms_for_table_agnostic_kept(semantic_dir):
    agnostic = SAMPLE.replace(
        "SUM(loan.amount) / COUNT(DISTINCT account.account_id)", "SUM(amount)")
    _write(semantic_dir, agnostic)
    p = SemanticLayerProvider(semantic_dir, "financial")

    hits = p.terms_for("avg loan", ["district"])  # 锚定表里没有 loan/account
    assert [h.term for h in hits] == ["avg_loan_per_account"]
