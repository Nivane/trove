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


# ── model() 必须只暴露通过校验的 metric ──────────────────────
#
# _validate 会丢弃「括号不配平 / SQLGlot 解析失败 / 引用未声明数据集」的
# metric;metrics() 返回校验后的表,model() 却曾返回未校验的 _parsed。于是
# 编译器拿 model() 对着 provider 自己都丢弃的条目做匹配,把坏表达式内联进
# 权威 SQL。model() 的 docstring 本就声明与 metrics() 同一缓存路径。

def test_model_metrics_exclude_dropped(semantic_dir, caplog):
    """括号不配平的 metric 被丢弃后,model() 也不得暴露它。"""
    _write(semantic_dir, SAMPLE.replace("SUM(loan.amount)", "SUM((", 1))
    p = SemanticLayerProvider(semantic_dir, "financial")

    with caplog.at_level("WARNING"):
        assert [m.name for m in p.metrics()] == ["avg_loan_per_account"]
    assert [m.name for m in p.model().metrics] == ["avg_loan_per_account"], (
        "model() 暴露了 _validate 已丢弃的 metric —— 编译器会拿它匹配并内联"
    )


def test_model_metrics_exclude_unknown_dataset(semantic_dir):
    """引用未声明数据集的 metric 同样不得出现在 model() 里。"""
    _write(semantic_dir, TWO_DATASET_METRICS)
    p = SemanticLayerProvider(
        semantic_dir, "financial", table_exists=lambda t: t == "account")

    assert [m.name for m in p.metrics()] == ["metric_on_account"]
    assert [m.name for m in p.model().metrics] == ["metric_on_account"]


def test_compiler_cannot_match_dropped_metric(semantic_dir, caplog):
    """端到端:编译器拿到 model() 后不得再匹配到已丢弃的 metric。

    修复前 result.sql 会把坏表达式 ``SUM((`` 内联进权威编译产物。
    """
    from trove.services.semantic_layer.compiler import (
        CompileMiss, CompileResult, SemanticCompiler,
    )

    _write(semantic_dir, SAMPLE.replace("SUM(loan.amount)", "SUM((", 1))
    p = SemanticLayerProvider(semantic_dir, "financial")
    plan = {"tables": ["loan"], "aggregation": "total_loan_amount",
            "answer_columns": ["total_loan_amount"]}

    with caplog.at_level("WARNING"):
        result = SemanticCompiler(p.model()).compile_detailed(plan, ["loan"])
    assert isinstance(result, CompileMiss), (
        f"已丢弃的 metric 仍被编译: {getattr(result, 'sql', result)}"
    )
    assert result.reason == "no_metric_match"


def test_compiler_still_matches_valid_metric(semantic_dir):
    """防过度拒绝:通过校验的 metric 照常可编译。"""
    from trove.services.semantic_layer.compiler import (
        CompileMiss, SemanticCompiler,
    )

    _write(semantic_dir, SAMPLE)
    p = SemanticLayerProvider(semantic_dir, "financial")
    plan = {"tables": ["loan"], "aggregation": "total_loan_amount",
            "answer_columns": ["total_loan_amount"]}

    result = SemanticCompiler(p.model()).compile_detailed(plan, ["loan"])
    assert not isinstance(result, CompileMiss), result
    assert "SUM(loan.amount)" in result.sql


def test_model_view_is_cached_and_keeps_model_level_fields(semantic_dir):
    """校验视图是缓存的同一对象,且模型级字段(instructions 等)不被丢掉。"""
    _write(semantic_dir, SAMPLE)
    p = SemanticLayerProvider(semantic_dir, "financial")

    first = p.model()
    assert first is p.model(), "每次调用都应返回缓存的同一视图,不重建"
    assert [d.name for d in first.datasets] == ["loan", "account"]
    assert first.instructions == "Use this model for banking and loan analysis"


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


# ── 模型级扩展面合并透传(此前被 merge 路径静默丢弃)────────────

SPINE_MODEL = """
version: "0.2.0.dev0"
semantic_model:
  - name: financial_analytics
    ai_context:
      examples: ["total loans by month"]
    custom_extensions:
      - vendor_name: trove
        data: "audit:2026"
    datasets:
      - name: loan
        source: financial.loan
        primary_key: [loan_id]
        fields:
          - name: date
            expression: {dialects: [{dialect: ANSI_SQL, expression: date}]}
    metrics:
      - name: total_amount
        expression: {dialects: [{dialect: ANSI_SQL, expression: SUM(loan.amount)}]}
    time_spine:
      field: loan.date
      granularity: month
      fill: "0"
"""


def test_merge_preserves_model_level_fields(tmp_path, semantic_dir):
    """KB(override)与配置目录(base)合并时,模型级字段不丢失。"""
    kb_path = Path(tmp_path) / "kb" / "financial" / "semantics.yml"
    kb_path.parent.mkdir(parents=True, exist_ok=True)
    kb_path.write_text(SPINE_MODEL, encoding="utf-8")
    _write(semantic_dir, SAMPLE)
    p = SemanticLayerProvider(semantic_dir, "financial", kb_semantics_path=kb_path)

    m = p.model()
    assert m is not None
    assert m.time_spine is not None
    assert m.time_spine.field == "loan.date"
    assert m.time_spine.granularity == "month"
    assert m.time_spine.fill == "0"
    assert m.version == "0.2.0.dev0"
    assert m.examples == ["total loans by month"]
    assert any(e["vendor_name"] == "trove" for e in m.custom_extensions)
    # 数据集/度量仍按名合并
    assert {d.name for d in m.datasets} == {"loan", "account"}
    assert {x.name for x in m.metrics} == {"total_loan_amount", "avg_loan_per_account", "total_amount"}


# ── 漂移检测(声明模型 vs 实时 catalog)──────────────────────

DRIFT_MODEL = """
semantic_model:
  - name: financial_analytics
    datasets:
      - name: loan
        source: financial.loan
        primary_key: [loan_id]
        fields:
          - name: amount
            expression: {dialects: [{dialect: ANSI_SQL, expression: amount}]}
          - name: account_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: account_id}]}
      - name: account
        source: financial.account
        primary_key: [account_id]
        fields:
          - name: account_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: account_id}]}
    relationships:
      - name: loan_account
        from: loan
        to: account
        from_columns: [account_id]
        to_columns: [account_id]
    metrics:
      - name: total_amount
        expression: {dialects: [{dialect: ANSI_SQL, expression: SUM(loan.amount)}]}
"""

_CLEAN_CATALOG = {
    "loan": {"amount", "account_id", "loan_id"},
    "account": {"account_id"},
}


def _drift_provider(tmp_path, catalog):
    kb_path = Path(tmp_path) / "kb" / "fin" / "semantics.yml"
    kb_path.parent.mkdir(parents=True, exist_ok=True)
    kb_path.write_text(DRIFT_MODEL, encoding="utf-8")
    return SemanticLayerProvider(
        Path(tmp_path) / "empty", "fin", kb_semantics_path=kb_path, catalog=catalog)


def test_drift_empty_without_catalog(tmp_path):
    p = SemanticLayerProvider(tmp_path / "missing", "financial")
    assert p.drift()["stale"] is False
    assert p.stale is False
    assert p.drift()["gone_tables"] == []


def test_drift_clean_model(tmp_path):
    p = _drift_provider(tmp_path, _CLEAN_CATALOG)
    report = p.drift()
    assert report["stale"] is False
    assert report["gone_tables"] == []
    assert report["missing_fields"] == {}
    assert report["missing_keys"] == {}
    assert report["relationship_breaks"] == []
    assert p.stale is False


def test_drift_gone_table(tmp_path):
    # catalog 缺 loan 表 → gone + 关系端点失效
    p = _drift_provider(tmp_path, {"account": {"account_id"}})
    report = p.drift()
    assert report["stale"] is True
    assert report["gone_tables"] == ["loan"]
    breaks = {b["name"] for b in report["relationship_breaks"]}
    assert breaks == {"loan_account"}


def test_drift_missing_fields_and_keys(tmp_path):
    # loan 表在,但 account_id/主键列缺失
    p = _drift_provider(tmp_path, {"loan": {"amount"}, "account": {"account_id"}})
    report = p.drift()
    assert report["stale"] is True
    assert report["missing_fields"] == {"loan": ["account_id"]}
    assert report["missing_keys"] == {"loan": ["loan_id"]}
    breaks = {b["name"] for b in report["relationship_breaks"]}
    assert breaks == {"loan_account"}


def test_drift_recomputed_on_reload(tmp_path):
    p = _drift_provider(tmp_path, _CLEAN_CATALOG)
    assert p.drift()["stale"] is False
    # 改写模型:新增不存在的表 → 下次访问重算
    kb_path = p._kb_path
    kb_path.write_text(DRIFT_MODEL.replace("financial.loan", "financial.gone"), encoding="utf-8")
    report = p.drift()
    assert report["stale"] is True
    assert report["gone_tables"] == ["loan"]


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
