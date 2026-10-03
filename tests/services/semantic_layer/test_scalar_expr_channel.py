"""A② 标量表达式列通道(闭语法 + 重建)。

计划把算式/带函数的标量列写进 ``answer_columns``(0482 的
``(district.A13 - district.A12) / district.A12 * 100``)时,旧投影循环对任何
含 ``(`` 的无签名列一律**静默跳过** —— 骨架丢列,生成侧无从知道它被丢。

新通道:sqlglot 严格解析 → 节点白名单 → 每列经 ``_resolve_field`` 落到声明
字段 → 用自己的渲染器**重建**文本(列强制表限定、函数名大写、字面量原样、
已有 CAST 原样保留;**绝不回放 LLM 原文、绝不自动补 CAST**)。闭语法刻意
收窄:聚合(AggFunc)、子查询、别名、未知函数、解析不到的列一律拒绝 ——
拒绝的代价是软 MISS(仅在"看着像算式"时记账),放宽的代价是权威错 SQL。
"""
from __future__ import annotations

from trove.services.semantic_layer.compiler import (
    CompileMiss,
    CompileResult,
    PartialCompile,
    SemanticCompiler,
    _looks_like_formula,
    scalar_expr_ref,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)


def _field(name, datatype=None):
    return SemanticField(name=name, expression=name, datatype=datatype)


def _model():
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="district", primary_key=["district_id"], fields=[
                _field("district_id"), _field("A2"), _field("A11"),
                _field("A12"), _field("A13"),
            ]),
            SemanticDataset(name="client", primary_key=["client_id"], fields=[
                _field("client_id"), _field("district_id"), _field("gender"),
                _field("birth_date", "Date"),
            ]),
        ],
        relationships=[
            SemanticRelationship("client_to_district", "client", "district",
                                 from_columns=["district_id"],
                                 to_columns=["district_id"], cardinality="1:N"),
        ],
        metrics=[
            SemanticMetric("number of clients", "COUNT(client.client_id)",
                           datasets=["client"]),
            SemanticMetric("row count", "COUNT(*)", datasets=["client"]),
        ],
    )


def _compile(plan, matched, dialect="mysql"):
    return SemanticCompiler(_model()).compile_detailed(
        plan, list(matched), force_dialect=dialect)


def _ref(expr: str) -> str | None:
    """单元入口:重建文本(或 None)。

    ``_matched_set`` 是解析期状态(compile_detailed 里赋值),单元调用直接
    按"计划命中了全部表"预置(通道只在编译期使用)。
    """
    compiler = SemanticCompiler(_model())
    compiler._matched_set = {"district", "client"}
    return scalar_expr_ref(expr, compiler)


# ══ 重建:表限定 / 括号规范化 / CAST 原样保留 ═════════════════


def test_formula_rebuilt_table_qualified():
    """0482 形状:算式列按闭语法**重建**(列强制表限定,括号由渲染器决定)。"""
    assert _ref("(district.A13 - district.A12) / district.A12 * 100") == (
        "(((district.A13 - district.A12) / district.A12) * 100)")


def test_unqualified_column_resolved_to_declared_field():
    """裸列引用经声明模型落表(唯一命中才认)。"""
    assert _ref("A13 - A12") == "(district.A13 - district.A12)"


def test_cast_preserved_verbatim_never_auto_added():
    """已有 CAST 原样保留(类型/精度);无 CAST 绝不自动补。"""
    assert _ref("CAST(district.A11 AS DOUBLE)") == "CAST(district.A11 AS DOUBLE)"
    assert _ref("district.A11 / 2") == "(district.A11 / 2)"


def test_whitelisted_functions_render_uppercase():
    assert _ref("round(district.A11, 2)") == "ROUND(district.A11, 2)"
    assert _ref("coalesce(district.A11, 0)") == "COALESCE(district.A11, 0)"
    assert _ref("year(district.district_id)") == "YEAR(district.district_id)"


def test_current_timestamp_and_date_render_parenthesized():
    """CURRENT_TIMESTAMP/CURRENT_DATE 按标准渲染成带括号的调用形式(0498 型
    年龄派生列:``YEAR(CURRENT_DATE) - YEAR(client.birth_date)``)。"""
    assert _ref("YEAR(CURRENT_DATE) - YEAR(client.birth_date)") == (
        "(YEAR(CURRENT_DATE()) - YEAR(client.birth_date))")
    # 纯时间常量不是"列"(至少引用一个声明字段才认)→ 整列弃用
    assert _ref("CURRENT_DATE") is None


# ══ 闭语法越界:一律 None(整列弃用,不是静默回放)════════════


def test_subquery_rejected():
    assert _ref("(SELECT MAX(district.A11) FROM district)") is None


def test_aggregate_rejected_by_scalar_channel():
    assert _ref("SUM(district.A11)") is None
    assert _ref("COUNT(*)") is None


def test_unknown_function_rejected():
    assert _ref("foo(district.A2)") is None


def test_undeclared_and_ambiguous_columns_rejected():
    assert _ref("district.ghost + 1") is None
    assert _ref("ghost.col") is None


def test_non_numeric_literal_rejected():
    assert _ref("district.A2 + 'x'") is None


def test_non_whitelisted_cast_type_rejected():
    assert _ref("CAST(district.A2 AS JSON)") is None


def test_alias_rejected():
    assert _ref("district.A11 AS x") is None


# ══ 投影循环接线 ════════════════════════════════════════════


def test_0482_shape_compiles_with_expression_projection():
    """0482 形状:维度 + 算式列 → 编译成功,算式列以重建文本进投影。"""
    plan = {
        "tables": ["district", "client"],
        "joins": "client.district_id = district.district_id",
        "answer_columns": [
            "district.A2",
            "(district.A13 - district.A12) / district.A12 * 100",
        ],
        "conditions": [{"field": "client.gender", "op": "=", "value": "'M'"}],
    }
    res = _compile(plan, ["district", "client"])
    assert isinstance(res, CompileResult), res
    assert res.sql == (
        "SELECT district.A2, (((district.A13 - district.A12) / district.A12) * 100)\n"
        "FROM district\n"
        "JOIN client ON client.district_id = district.district_id\n"
        "WHERE client.gender = 'M'"
    )


def test_cast_variant_keeps_cast_in_skeleton():
    """CAST 变体:计划自带 CAST 时产物保留 CAST(骨架保真校验要求逐字复现)。"""
    plan = {
        "tables": ["district", "client"],
        "joins": "client.district_id = district.district_id",
        "answer_columns": ["district.A2", "CAST(district.A11 AS DOUBLE) / 2"],
    }
    res = _compile(plan, ["district", "client"])
    assert isinstance(res, CompileResult), res
    assert "(CAST(district.A11 AS DOUBLE) / 2)" in res.sql


def test_formula_column_pulls_table_into_needed():
    """算式列引用的数据集自动进 needed(表格漏列也接得上声明连边)。"""
    plan = {
        "tables": ["client"],
        "answer_columns": [
            "client.client_id",
            "(district.A13 - district.A12) / district.A12 * 100",
        ],
    }
    res = _compile(plan, ["client", "district"])
    assert isinstance(res, CompileResult), res
    assert "JOIN district ON client.district_id = district.district_id" in res.sql


def test_bare_star_placeholders_stay_silent():
    """``number(*)`` 这类无实参占位符:静默跳过(计划噪声,同旧行为)。"""
    plan = {
        "tables": ["district"],
        "answer_columns": ["district.A2", "number(*)"],
    }
    res = _compile(plan, ["district"])
    assert isinstance(res, CompileResult), res
    assert res.sql == "SELECT district.A2\nFROM district"


def test_aggregate_subquery_column_soft_miss_via_candidate_channel():
    """``(SELECT MAX(...) ...)`` 列:含聚合 → 进候选池,签名不兼容 → 软 MISS
    ``no_metric_match``(绝不静默丢、也绝不回放子查询文本)。"""
    plan = {
        "tables": ["district"],
        "answer_columns": ["district.A2", "(SELECT MAX(district.A11) FROM district)"],
    }
    res = _compile(plan, ["district"])
    assert isinstance(res, PartialCompile), res
    assert res.miss_parts == [
        {"reason": "no_metric_match",
         "component": "(SELECT MAX(district.A11) FROM district)"}]
    assert "SELECT MAX" not in res.sql


def test_plain_subquery_column_soft_miss():
    """``(SELECT district.A11 ...)`` 列(无聚合,签名为空):标量通道外 + 形态
    像列 → 软 MISS ``unresolved_answer_column``。"""
    plan = {
        "tables": ["district"],
        "answer_columns": ["district.A2", "(SELECT district.A11 FROM district)"],
    }
    res = _compile(plan, ["district"])
    assert isinstance(res, PartialCompile), res
    assert {"reason": "unresolved_answer_column",
            "component": "(SELECT district.A11 FROM district)"} in res.miss_parts
    assert "SELECT district.A11 FROM district" not in res.sql


def test_unknown_function_column_soft_miss():
    """``foo(district.A2)``:形态像函数列 → 软 MISS(不是静默丢)。"""
    plan = {
        "tables": ["district"],
        "answer_columns": ["district.A2", "foo(district.A2)"],
    }
    res = _compile(plan, ["district"])
    assert isinstance(res, PartialCompile), res
    assert {"reason": "unresolved_answer_column", "component": "foo(district.A2)"} \
        in res.miss_parts


def test_count_star_still_goes_through_metric_channel():
    """``count(*)`` 仍是聚合候选 → 走度量签名对账(AggFunc 被标量通道拒绝,
    绝不把它当标量列重建):产物是**声明度量**的表达式,而非回放候选文本。"""
    plan = {
        "tables": ["client"],
        "aggregation": "count(*)",
        "answer_columns": ["count(*)"],
    }
    res = _compile(plan, ["client"])
    assert isinstance(res, CompileResult), res
    # 签名对账命中声明度量(COUNT(*) 与声明计数度量同签名)→ 内联声明表达式
    assert res.sql == "SELECT COUNT(client.client_id)\nFROM client"


def test_formula_detection_judgement():
    """形态判定:算式/函数列(含子查询)→ True;占位符 → False。"""
    assert _looks_like_formula("(SELECT 1)")
    assert _looks_like_formula("a + b")
    assert _looks_like_formula("CAST(x AS DOUBLE)")
    assert _looks_like_formula("foo(district.A2)")
    assert not _looks_like_formula("number(*)")
    assert not _looks_like_formula("min(*)")


def test_parenless_formula_keeps_existing_soft_miss_path():
    """**无括号**算式(``district.A11 / 100``)不进标量通道(分发判据是
    "含 ``(``" 的既有形状),走旧的列解析失败路径 → 软 MISS 而非静默丢。

    这是刻意的边界:旧路径本就记 ``unresolved_answer_column``(软缺口,gen
    照常接手),扩大通道面没有收益,只增加权威 SQL 的表达面。
    """
    plan = {
        "tables": ["district"],
        "answer_columns": ["district.A11 / 100"],
    }
    res = _compile(plan, ["district"])
    assert isinstance(res, CompileMiss), res
    assert res.reason == "unresolved_answer_column"

    # 加一层括号即进通道 → 进投影(判断依据是形状,不是语义)
    plan_paren = {
        "tables": ["district"],
        "answer_columns": ["(district.A11) / 100"],
    }
    res_paren = _compile(plan_paren, ["district"])
    assert isinstance(res_paren, CompileResult), res_paren
    assert res_paren.sql == "SELECT (district.A11 / 100)\nFROM district"
