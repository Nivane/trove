"""计划层 joins 声明图修复(``repair_plan_joins``,B4)。

0493 实测:计划写了一条**未声明**的连接(``account.district_id =
client.district_id``,共享维度桥)。编译器的显式 joins 通道能在编译内部把它
还原成声明路径(account—disp—client),但编译若因**其它组件**软 MISS,plan
文本会带着坏连接原样交给 gen_sql —— 生成侧照抄,错误被固化。修复前移到计划
层:编译与生成两侧看到的都是合规 joins。

规则与 ``_repair_explicit_joins`` 同源,保守性一致:修不出来(无路径/多路径/
不可解析)→ 整份计划不动(交既有硬 MISS + 有界重规划,不猜)。
"""
from __future__ import annotations

from trove.services.semantic_layer.compiler import (
    declared_join_edge_text,
    repair_plan_joins,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticModel,
    SemanticRelationship,
)


def _field(name):
    return SemanticField(name=name, expression=name)


def _model() -> SemanticModel:
    """financial 骨架裁剪:account—disp—client 所有权链 + 共享维度 district。"""
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="account", primary_key=["account_id"], fields=[
                _field("account_id"), _field("district_id"),
            ]),
            SemanticDataset(name="disp", primary_key=["disp_id"], fields=[
                _field("disp_id"), _field("account_id"), _field("client_id"),
            ]),
            SemanticDataset(name="client", primary_key=["client_id"], fields=[
                _field("client_id"), _field("district_id"), _field("gender"),
            ]),
            SemanticDataset(name="district", primary_key=["district_id"], fields=[
                _field("district_id"), _field("A2"),
            ]),
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("account_id"), _field("amount"),
            ]),
        ],
        relationships=[
            SemanticRelationship("disp_to_account", "disp", "account",
                                 from_columns=["account_id"],
                                 to_columns=["account_id"], cardinality="1:1"),
            SemanticRelationship("disp_to_client", "disp", "client",
                                 from_columns=["client_id"],
                                 to_columns=["client_id"], cardinality="1:1"),
            SemanticRelationship("account_to_district", "account", "district",
                                 from_columns=["district_id"],
                                 to_columns=["district_id"], cardinality="1:N"),
            SemanticRelationship("client_to_district", "client", "district",
                                 from_columns=["district_id"],
                                 to_columns=["district_id"], cardinality="1:N"),
            SemanticRelationship("loan_to_account", "loan", "account",
                                 from_columns=["account_id"],
                                 to_columns=["account_id"], cardinality="1:N"),
        ],
    )


def _plan(**kw) -> dict:
    base = {
        "tables": ["loan", "account", "client"],
        "joins": (
            "loan.account_id = account.account_id AND "
            "account.district_id = client.district_id"
        ),
        "conditions": [{"field": "client.gender", "op": "=", "value": "'M'"}],
        "aggregation": "sum",
        "answer_columns": ["sum(loan.amount)"],
        "having": [],
        "plan_field": "",
    }
    base.update(kw)
    return base


# ══ 0493 逐字形状:坏连接 → 声明路径 ═══════════════════════


def test_undeclared_dimension_bridge_repaired_to_ownership_chain():
    """共享维度桥(account—district—client)不可穿行(district 是纯维度叶),
    唯一可走的是所有权链 account—disp—client。"""
    fixed = repair_plan_joins(_plan(), _model())
    assert fixed is not None
    assert fixed["joins"] == (
        "loan.account_id = account.account_id AND "
        "disp.account_id = account.account_id AND "
        "disp.client_id = client.client_id"
    )
    assert fixed["tables"] == ["loan", "account", "client", "disp"]
    assert fixed["plan_field"] == "repair_plan_joins"


def test_repair_only_touches_joins_tables_plan_field():
    """其余组件逐字保留(条件/聚合/投影/limit 不因修复而变)。"""
    plan = _plan(limit=9, ordering=[{"column": "sum(loan.amount)", "direction": "desc"}])
    fixed = repair_plan_joins(plan, _model())
    assert fixed is not None
    for key in ("conditions", "aggregation", "answer_columns", "having", "limit", "ordering"):
        assert fixed[key] == plan[key]
    assert plan["joins"].count("client.district_id") == 1  # 原计划未被就地改写
    assert "client.district_id" not in fixed["joins"]


def test_repaired_plan_is_idempotent():
    """修复产物再修一次 → None(全部子句已合规,无需改动)。"""
    once = repair_plan_joins(_plan(), _model())
    assert once is not None
    assert repair_plan_joins(once, _model()) is None


# ══ 保守边界:不猜 ═══════════════════════════════════════


def test_all_declared_clauses_untouched():
    """全部子句已合规 → None(不改写,连大小写/写法都保留)。"""
    plan = _plan(joins=(
        "loan.account_id = account.account_id AND "
        "disp.account_id = account.account_id AND disp.client_id = client.client_id"
    ), tables=["loan", "account", "disp", "client"])
    assert repair_plan_joins(plan, _model()) is None


def test_unknown_table_not_repaired():
    """表对含未声明表 → None(不猜表)。"""
    plan = _plan(joins="loan.account_id = ghost.account_id")
    assert repair_plan_joins(plan, _model()) is None


def test_unparseable_clause_not_repaired():
    """子句不可解析(非列对列)→ None。"""
    plan = _plan(joins="loan.account_id = account.account_id AND 1 = 1")
    assert repair_plan_joins(plan, _model()) is None


def test_direct_relationship_beats_path_search():
    """表对存在**直接**声明关系(哪怕列写错)→ 直接用关系边,不走路径搜索。"""
    model = _model()
    model.relationships.append(
        SemanticRelationship("client_to_account", "client", "account",
                             from_columns=["client_id"], to_columns=["account_id"],
                             cardinality="1:1"),
    )
    fixed = repair_plan_joins(_plan(joins="account.district_id = client.district_id"), model)
    assert fixed is not None
    assert fixed["joins"] == "client.client_id = account.account_id"


def test_ambiguous_pair_not_repaired():
    """表对存在两条同长声明路径(菱形)→ None,整份计划不动。"""
    model = _model()
    # 与 account—disp—client 并行的第二条 2 跳链 account—mandate—client
    model.datasets.extend([
        SemanticDataset(name="mandate", primary_key=["mandate_id"],
                        fields=[_field("mandate_id"), _field("account_id"),
                                _field("client_id")]),
    ])
    model.relationships.extend([
        SemanticRelationship("mandate_to_account", "mandate", "account",
                             from_columns=["account_id"], to_columns=["account_id"],
                             cardinality="1:1"),
        SemanticRelationship("mandate_to_client", "mandate", "client",
                             from_columns=["client_id"], to_columns=["client_id"],
                             cardinality="1:1"),
    ])
    plan = _plan(joins="account.district_id = client.district_id")
    assert repair_plan_joins(plan, model) is None


def test_placeholder_joins_untouched():
    """占位/空 joins → None(交 BFS 通道,同旧行为)。"""
    assert repair_plan_joins(_plan(joins=""), _model()) is None
    assert repair_plan_joins(_plan(joins="none"), _model()) is None
    assert repair_plan_joins(_plan(joins=None), _model()) is None


def test_no_model_or_non_dict_untouched():
    assert repair_plan_joins(_plan(), None) is None
    assert repair_plan_joins(None, _model()) is None


# ══ declared_join_edge_text(车道 B 的重锚接口)═════════════


def test_declared_join_edge_text_unique_and_missing():
    model = _model()
    assert declared_join_edge_text(model, "client", "district") == (
        "client.district_id = district.district_id")
    assert declared_join_edge_text(model, "account", "client") is None  # 无直接关系
