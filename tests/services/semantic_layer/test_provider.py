"""SemanticLayerProvider tests: mtime cache, validation, last-known-good.

The provider reads OSSIE YAML files live from a directory (per-datasource),
re-parsing only when a file's mtime/size changes, validating each metric
expression, and keeping the last known good model when a file breaks.
"""
import pytest
from pathlib import Path

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


def test_model_exposes_datasets_and_relationships(semantic_dir):
    _write(semantic_dir, SAMPLE)
    p = SemanticLayerProvider(semantic_dir, "financial")

    model = p.model()
    assert model is not None
    assert [d.name for d in model.datasets] == ["loan", "account"]
    assert model.relationships == []  # SAMPLE 未声明 relationships

    assert p.model() is not None  # 缓存路径不发散


def test_model_none_when_disabled(tmp_path):
    p = SemanticLayerProvider(tmp_path / "missing", "financial")
    assert p.model() is None


# ── 单一真源:KB semantics.yml 合并(P4)──────────────────────

KB_MODEL = """
semantic_model:
  - name: financial_analytics
    datasets:
      - name: district
        source: financial.district
        primary_key: [district_id]
        fields:
          - name: A3
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: A3
            datatype: String
            description: district name
            ai_context:
              synonyms: [region, area]
    metrics:
      - name: total_loan_amount
        description: KB authoritative
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(loan.amount) * 2
        ai_context:
          synonyms: [total loans]
"""


def _kb_path(tmp_path) -> Path:
    p = Path(tmp_path) / "kb" / "financial" / "semantics.yml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(KB_MODEL, encoding="utf-8")
    return p


def _role_path(tmp_path) -> Path:
    p = Path(tmp_path) / "kb" / "fin" / "semantics.yml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(ROLE_MODEL, encoding="utf-8")
    return p


def test_enabled_via_kb_semantics_alone(tmp_path):
    """配置目录为空,KB semantics.yml 存在 → 启用且模型字段可达。"""
    p = SemanticLayerProvider(tmp_path / "empty", "financial",
                              kb_semantics_path=_kb_path(tmp_path))
    assert p.enabled is True

    model = p.model()
    assert model is not None
    district = next(d for d in model.datasets if d.name == "district")
    a3 = next(f for f in district.fields if f.name == "A3")
    assert a3.synonyms == ["region", "area"]
    assert a3.datatype == "String"


def test_kb_metric_overrides_directory_source(tmp_path, semantic_dir):
    """同名 metric:KB(真源)覆盖配置目录演示资产。"""
    _write(semantic_dir, SAMPLE.replace(
        "description: Total amount of all loans",
        "description: demo asset (should lose)"))
    p = SemanticLayerProvider(semantic_dir, "financial",
                              kb_semantics_path=_kb_path(tmp_path))
    metrics = {m.name: m for m in p.metrics()}
    assert metrics["total_loan_amount"].expression == "SUM(loan.amount) * 2"
    assert metrics["total_loan_amount"].definition == "KB authoritative"


def test_field_hits_maps_question_word_to_field(tmp_path):
    p = SemanticLayerProvider(tmp_path / "empty", "financial",
                              kb_semantics_path=_kb_path(tmp_path))

    hits = p.field_hits("What is the average loan per region?", tables=["district"])
    assert hits == ["'region' → district.A3"]

    assert p.field_hits("how many accounts?", tables=["district"]) == []
    assert p.field_hits("average loan per region", tables=["loan"]) == []


# ── P5.1: 倒排字段候选 + 语义角色 ─────────────────────────

ROLE_MODEL = """
semantic_model:
  - name: fin
    datasets:
      - name: district
        fields:
          - name: A3
            expression: {dialects: [{dialect: ANSI_SQL, expression: A3}]}
            ai_context: {synonyms: [region, area]}
            semantic_role: dimension
          - name: district_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: district_id}]}
            semantic_role: identifier
          - name: A11
            expression: {dialects: [{dialect: ANSI_SQL, expression: A11}]}
            ai_context: {synonyms: [avg salary]}
            semantic_role: measure
      - name: loan
        fields:
          - name: status
            expression: {dialects: [{dialect: ANSI_SQL, expression: status}]}
            enum_display: {A: finished, B: running}
            ai_context: {synonyms: [repayment state]}
"""


def test_field_candidates_via_inverted_index(tmp_path):
    p = SemanticLayerProvider(tmp_path / "empty", "fin",
                              kb_semantics_path=_role_path(tmp_path))

    cands = p.field_candidates("average salary per region", tables=["district"])
    terms = {(d, f) for d, f, _ in cands}
    assert ("district", "A11") in terms   # salary synonym hit
    assert ("district", "A3") in terms    # region synonym hit

    # 表级锚定:只回命中表
    assert all(d == "district" for d, f, _ in cands)


def test_field_candidates_unmatched_question_empty(tmp_path):
    p = SemanticLayerProvider(tmp_path / "empty", "fin",
                              kb_semantics_path=_role_path(tmp_path))
    assert p.field_candidates("how many zebras?", tables=["district"]) == []


def test_parse_semantic_role_and_enum_display(tmp_path):
    p = SemanticLayerProvider(tmp_path / "empty", "fin",
                              kb_semantics_path=_role_path(tmp_path))
    model = p.model()
    district = next(d for d in model.datasets if d.name == "district")
    by_name = {f.name: f for f in district.fields}
    assert by_name["A3"].semantic_role == "dimension"
    assert by_name["district_id"].semantic_role == "identifier"
    assert by_name["A11"].semantic_role == "measure"

    loan = next(d for d in model.datasets if d.name == "loan")
    status = next(f for f in loan.fields if f.name == "status")
    assert status.enum_display == {"A": "finished", "B": "running"}


DERIVED_MODEL = """
semantic_model:
  - name: financial_analytics
    datasets:
      - name: loan
        source: financial.loan
    metrics:
      - name: total_loan_amount
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(loan.amount)
      - name: avg_per_loan
        type: derived
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: total_loan_amount / COUNT(loan.loan_id)
"""


def test_derived_metric_survives_validation(semantic_dir):
    # 派生表达式(裸列引用 metric 名)可解析 → 通过 _validate;metric_type 落模型
    _write(semantic_dir, DERIVED_MODEL)
    p = SemanticLayerProvider(semantic_dir, "financial")
    assert p.enabled
    model = p.model()
    assert model is not None
    by_name = {m.name: m for m in model.metrics}
    assert by_name["avg_per_loan"].metric_type == "derived"
    assert by_name["total_loan_amount"].metric_type == ""


def test_unparseable_derived_metric_still_dropped(semantic_dir):
    _write(semantic_dir, """
semantic_model:
  - name: financial_analytics
    datasets:
      - name: loan
        source: financial.loan
    metrics:
      - name: bad_derived
        type: derived
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: "SUM(loan.amount) / / /"
""")
    p = SemanticLayerProvider(semantic_dir, "financial")
    # metrics() 返回校验后列表(model() 是未校验的解析结果)
    assert all(m.name != "bad_derived" for m in p.metrics())
