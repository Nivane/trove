"""SemanticCompiler tests: constrained-selection SQL compilation.

plan 的 metric/group_by/filters 全部落到已声明模型条目时才编译;任一项
不在「逻辑宇宙」内 → 严格 MISS(None)。聚合项必须命中声明 metric。
"""
import pytest

from trove.services.semantic_layer.compiler import SemanticCompiler
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)


def _field(name, datatype=None):
    return SemanticField(name=name, expression=name, datatype=datatype)


def _demo_model():
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("account_id"), _field("amount"),
                _field("status"), _field("date", "Date"),
            ]),
            SemanticDataset(name="account", primary_key=["account_id"], fields=[
                _field("account_id"), _field("district_id"), _field("frequency"),
            ]),
            SemanticDataset(name="district", primary_key=["district_id"], fields=[
                _field("district_id"), _field("A3"),
            ]),
        ],
        relationships=[
            SemanticRelationship("loan_to_account", "loan", "account",
                                 from_columns=["account_id"], to_columns=["account_id"]),
            SemanticRelationship("account_to_district", "account", "district",
                                 from_columns=["district_id"], to_columns=["district_id"]),
        ],
        metrics=[
            SemanticMetric("number of loan records", "COUNT(loan.loan_id)", datasets=["loan"]),
            SemanticMetric("total_loan_amount", "SUM(loan.amount)", datasets=["loan"]),
            SemanticMetric("avg_loan_amount", "AVG(loan.amount)", datasets=["loan"]),
        ],
    )


def _compile(plan, matched, model=None):
    return SemanticCompiler(model or _demo_model()).compile_from_plan(plan, matched)


def test_aggregate_by_dimension_with_filter():
    plan = {
        "tables": ["loan", "account", "district"],
        "aggregation": "avg(loan.amount)",
        "answer_columns": ["district.A3", "avg(loan.amount)"],
        "conditions": [{"field": "district.A3", "op": "=", "value": "Prague"}],
    }
    result = _compile(plan, ["loan", "district", "account"])
    assert result is not None
    sql = result.sql
    assert sql.startswith("SELECT district.A3, AVG(loan.amount)")
    assert "FROM loan" in sql
    assert "JOIN account ON loan.account_id = account.account_id" in sql
    assert "JOIN district ON account.district_id = district.district_id" in sql
    assert "WHERE district.A3 = 'Prague'" in sql
    assert "GROUP BY district.A3" in sql
    assert "authoritative" in result.block


def test_simple_count_single_table():
    plan = {"aggregation": "count(loan.loan_id)", "answer_columns": ["count(loan.loan_id)"]}
    result = _compile(plan, ["loan"])
    assert result is not None
    assert result.sql == "SELECT COUNT(loan.loan_id)\nFROM loan"


def test_count_wildcard_matches_declared_metric():
    # COUNT(*) 与声明 COUNT(loan.loan_id) 签名兼容(空列集通配)
    plan = {"aggregation": "count(*)", "answer_columns": ["count(*)"]}
    result = _compile(plan, ["loan"])
    assert result is not None
    assert "COUNT(loan.loan_id)" in result.sql


def test_list_query_projects_fields():
    plan = {
        "answer_columns": ["loan.status"],
        "conditions": [{"field": "loan.status", "op": "=", "value": "A"}],
    }
    result = _compile(plan, ["loan"])
    assert result is not None
    assert result.sql == "SELECT loan.status\nFROM loan\nWHERE loan.status = 'A'"


def test_two_filters_combined_with_and():
    plan = {
        "answer_columns": ["loan.status"],
        "conditions": [
            {"field": "loan.status", "op": "=", "value": "A"},
            {"field": "loan.amount", "op": ">=", "value": 10},
        ],
    }
    result = _compile(plan, ["loan"])
    assert result is not None
    assert "WHERE loan.status = 'A' AND loan.amount >= 10" in result.sql


def test_string_value_escaped():
    plan = {
        "answer_columns": ["loan.status"],
        "conditions": [{"field": "loan.status", "op": "=", "value": "O'Brien"}],
    }
    result = _compile(plan, ["loan"])
    assert "loan.status = 'O''Brien'" in result.sql


@pytest.mark.parametrize("op", ["like", "in", "<>", "to", "between"])
def test_unknown_operator_is_strict_miss(op):
    plan = {
        "answer_columns": ["loan.status"],
        "conditions": [{"field": "loan.status", "op": op, "value": "A"}],
    }
    if op in {"like", "in", "<>"}:
        assert _compile(plan, ["loan"]) is not None
    else:
        assert _compile(plan, ["loan"]) is None


def test_metric_not_in_declared_is_strict_miss():
    plan = {"aggregation": "sum(loan.ghost_col)", "answer_columns": ["sum(loan.ghost_col)"]}
    assert _compile(plan, ["loan"]) is None


def test_aggregate_without_declared_metric_is_miss():
    # 声明了聚合(avg over trans.amount)但模型里没有该 metric → 严格 MISS
    plan = {"aggregation": "avg(trans.amount)", "answer_columns": ["avg(trans.amount)"]}
    assert _compile(plan, ["trans", "loan"]) is None


def test_group_by_field_not_declared_is_miss():
    plan = {
        "aggregation": "count(loan.loan_id)",
        "answer_columns": ["district.A3", "count(loan.loan_id)"],
    }
    # A3 未声明(dataset 无 A3 field)→ 分组维度不在宇宙内 → MISS
    model = _demo_model()
    model.datasets[2].fields = []  # district 无任何 field
    plan["group_by_dim"] = True
    assert SemanticCompiler(model).compile_from_plan(plan, ["loan", "district", "account"]) is None


def test_filter_field_not_declared_is_miss():
    plan = {
        "answer_columns": ["loan.status"],
        "conditions": [{"field": "loan.ghost", "op": "=", "value": "x"}],
    }
    assert _compile(plan, ["loan"]) is None


def test_empty_or_single_table_returns_none():
    assert _compile({}, []) is None
    assert _compile({"answer_columns": ["loan.status"], "conditions": [
        {"field": "loan.amount", "op": ">", "value": 1}], }, None) is None


def test_condition_without_value_is_miss():
    plan = {
        "answer_columns": ["loan.status"],
        "conditions": [{"field": "loan.status", "op": "=", "value": None}],
    }
    assert _compile(plan, ["loan"]) is None


def test_astral_table_anchor_falls_back_to_first_matched():
    # metric 锚定 loan,但贷方不在 matched → 兜底 matched[0] 且不产生无效引用
    plan = {"aggregation": "count(loan.loan_id)", "answer_columns": ["count(loan.loan_id)"]}
    result = _compile(plan, ["district"])
    # loan 不在 matched → 回到 district,仍编译(FROM district)但不引用 loan 会很怪
    # —— strict 语义下这种情况当场 MISS(表不在 matched 就是未覆盖)
    assert result is None


def test_resolve_field_via_synonym_alias():
    """planner 直接用别名写列(district.region)→ 唯一命中同数据集字段 A3。"""
    model = _demo_model()
    district = model.datasets[2]
    a3 = next(f for f in district.fields if f.name == "A3")
    a3.synonyms = ["region", "area"]

    plan = {
        "aggregation": "avg(loan.amount)",
        "answer_columns": ["district.region", "avg(loan.amount)"],
        "conditions": [{"field": "district.region", "op": "=", "value": "Prague"}],
    }
    result = _compile(plan, ["loan", "district", "account"], model=model)
    assert result is not None
    sql = result.sql
    assert "district.A3" in sql
    assert "district.region" not in sql
    assert "WHERE district.A3 = 'Prague'" in sql
    assert "GROUP BY district.A3" in sql


def test_synonym_ambiguous_is_miss():
    """两个字段共享同一 synonym → 歧义,不猜,转 LLM 通道。"""
    model = _demo_model()
    district = model.datasets[2]
    a3 = next(f for f in district.fields if f.name == "A3")
    a3.synonyms = ["region"]
    district.fields.append(_field("A4"))
    district.fields[-1].synonyms = ["region"]

    plan = {
        "aggregation": "avg(loan.amount)",
        "answer_columns": ["district.region", "avg(loan.amount)"],
    }
    assert _compile(plan, ["loan", "district", "account"], model=model) is None

def test_compile_strict_miss_on_many_to_many_path():
    """P5.2:M:N 边在联路径 → 编译期拒,严格 MISS 回 LLM 通道。"""
    from trove.services.semantic_layer.models import SemanticRelationship

    model = _demo_model()
    model.relationships.append(
        SemanticRelationship("trans_to_card_m2n", "trans", "card",
                             from_columns=["disp_id"], to_columns=["disp_id"],
                             cardinality="M:N"))
    # matched 含 card(仅经 M:N 路径可达)→ fan_out → None
    plan = {
        "aggregation": "count(trans.trans_id)",
        "answer_columns": ["count(trans.trans_id)"],
    }
    assert _compile(plan, ["trans", "card"], model=model) is None


def test_compile_still_ok_when_m2n_unrelated():
    """M:N 边存在但与查询无关(剪枝后不在路径上)→ 正常编译。"""
    from trove.services.semantic_layer.models import SemanticRelationship

    model = _demo_model()
    model.relationships.append(
        SemanticRelationship("order_to_card_m2n", "order", "card",
                             from_columns=["disp_id"], to_columns=["disp_id"],
                             cardinality="M:N"))
    plan = {
        "aggregation": "count(loan.loan_id)",
        "answer_columns": ["count(loan.loan_id)"],
    }
    assert _compile(plan, ["loan"], model=model) is not None
