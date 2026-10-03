"""A① ``plan.extreme`` 消费:极值声明的三条落点 + 保守边界。

改造前 query_sketch 的 ``extreme``(“最高/最低/第 n 高的那一行”)被编译器
**整体忽略**:骨架既没有 ``ORDER BY … LIMIT 1``,也没有行级选择谓词 —— 计划
里的"选哪一行"语义被静默丢给生成侧,骨架与计划在"选哪一行"上不一致
(0475 的 ``min(*)`` 连投影兜底都没有,整份计划以 nothing_compilable 收场)。

消费规则刻意保守,越界一律**软 MISS**(绝不硬拒、绝不静默丢):

- 投影为空 → 兜底 ``FUNC(极值列)`` 作唯一投影;
- 投影已含 ``FUNC(极值列)`` → 已表达,不动(叠加 LIMIT 1 会把多组聚合
  截成一条);
- ``rank ≥ 2``,或聚合投影不含极值列 → 行级选择谓词
  ``col = (SELECT col FROM t [WHERE 同表条件] ORDER BY col DESC LIMIT 1
  [OFFSET n-1])``;
- 纯维度投影 + ``rank = 1`` → ``ORDER BY col DESC`` + ``LIMIT 1``(计划显式
  给了 limit 则保留其值);
- 显式 ``ordering`` 存在 → 整体跳过(更强的呈现声明优先,叠加会互相矛盾);
- ``analysis`` 存在 → 静默忽略(窗口包装自己决定排序,只记 log 不记软 MISS)。

模型合成,断言只钉形状与语义。
"""
from __future__ import annotations

from trove.services.semantic_layer.compiler import (
    CompileMiss,
    CompileResult,
    PartialCompile,
    SemanticCompiler,
    _extreme_rank,
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
    """district(一) ← client(多);A11/A15 作极值列候选。"""
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="district", primary_key=["district_id"], fields=[
                _field("district_id"), _field("A2"), _field("A11"), _field("A15"),
            ]),
            SemanticDataset(name="client", primary_key=["client_id"], fields=[
                _field("client_id"), _field("district_id"), _field("gender"),
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
            SemanticMetric("number of districts", "COUNT(district.district_id)",
                           datasets=["district"]),
            # 投影已表达极值本身的声明度量(max A11)
            SemanticMetric("max A11", "MAX(district.A11)", datasets=["district"]),
        ],
    )


def _compile(plan, matched, dialect="mysql"):
    return SemanticCompiler(_model()).compile_detailed(
        plan, list(matched), force_dialect=dialect)


def _sql(result) -> str:
    assert not isinstance(result, CompileMiss), f"unexpected MISS: {result}"
    return result.sql


# ══ rank = 1 + 纯维度投影 → ORDER BY + LIMIT 1 ════════════════


def test_rank1_dimension_projection_orders_desc_and_limits_1():
    """"最高的 X 那一行"的维度投影:排序首位 + LIMIT 1(骨架可执行)。"""
    plan = {
        "tables": ["district"],
        "answer_columns": ["district.A2"],
        "extreme": {"func": "max", "column": "district.A11"},
    }
    res = _compile(plan, ["district"])
    assert _sql(res) == (
        "SELECT district.A2\n"
        "FROM district\n"
        "ORDER BY district.A11 DESC\n"
        "LIMIT 1"
    )


def test_rank1_min_orders_asc():
    """min → ASC(极值方向由 func 决定)。"""
    plan = {
        "tables": ["district"],
        "answer_columns": ["district.A2"],
        "extreme": {"func": "min", "column": "district.A11"},
    }
    assert "ORDER BY district.A11 ASC" in _sql(_compile(plan, ["district"]))


def test_rank1_keeps_plan_limit():
    """计划显式给了 limit → 不覆盖(取前 N 名),ORDER BY 仍在首位。"""
    plan = {
        "tables": ["district"],
        "answer_columns": ["district.A2"],
        "extreme": {"func": "max", "column": "district.A11"},
        "limit": 5,
    }
    sql = _sql(_compile(plan, ["district"]))
    assert sql.endswith("ORDER BY district.A11 DESC\nLIMIT 5")


def test_rank1_illegal_rank_defaults_to_one():
    """rank 非法(非数/0/负)→ 按 1 处理(不因噪声字段整列弃用)。"""
    for bad in ("x", 0, -2, None):
        plan = {
            "tables": ["district"],
            "answer_columns": ["district.A2"],
            "extreme": {"func": "max", "column": "district.A11", "rank": bad},
        }
        assert "LIMIT 1" in _sql(_compile(plan, ["district"])), bad
    assert _extreme_rank({"rank": "3"}) == 3
    assert _extreme_rank({"rank": "nope"}) == 1


# ══ rank ≥ 2 → 行级选择谓词(0486 手写形状)═══════════════════


def test_rank2_selects_second_extreme_via_subquery():
    """第 2 高的行:``col = (SELECT col FROM t ORDER BY col DESC LIMIT 1 OFFSET 1)``。

    0486 手写形状(plan fixture 见 test_sem_mech_integration.py):ORDER BY+
    LIMIT n 在多组聚合下不可表达,行级谓词才是无歧义形式。
    """
    plan = {
        "tables": ["district", "client"],
        "joins": "client.district_id = district.district_id",
        "aggregation": "number of clients",
        "answer_columns": ["number of clients"],
        "extreme": {"func": "max", "column": "district.A15", "rank": 2},
    }
    sql = _sql(_compile(plan, ["client", "district"]))
    assert (
        "district.A15 = (SELECT district.A15 FROM district "
        "ORDER BY district.A15 DESC LIMIT 1 OFFSET 1)"
    ) in sql


def test_rank2_copies_same_table_conditions_into_subquery():
    """同表条件复制进子查询:极值限定在计划声明的过滤集内(不是全表极值)。"""
    plan = {
        "tables": ["district"],
        "aggregation": "number of districts",
        "answer_columns": ["number of districts"],
        "conditions": [{"field": "district.A11", "op": ">", "value": 10000}],
        "extreme": {"func": "max", "column": "district.A15", "rank": 2},
    }
    sql = _sql(_compile(plan, ["district"]))
    assert (
        "district.A15 = (SELECT district.A15 FROM district "
        "WHERE district.A11 > 10000 "
        "ORDER BY district.A15 DESC LIMIT 1 OFFSET 1)"
    ) in sql


def test_cross_table_condition_without_global_scope_soft_miss():
    """条件落在别的表上且 scope 未声明全局 → 软 MISS ``extreme_rank_scope_unsupported``。

    跨表条件无法复制进子查询;不复制就会在**未过滤集**上取极值(张冠李戴,
    比没有谓词更坏)。此时放弃谓词(软 MISS,其余编译照常),不外泄错 SQL。
    """
    plan = {
        "tables": ["district", "client"],
        "joins": "client.district_id = district.district_id",
        "aggregation": "number of clients",
        "answer_columns": ["number of clients"],
        "conditions": [{"field": "client.gender", "op": "=", "value": "'F'"}],
        "extreme": {"func": "max", "column": "district.A15", "rank": 2},
    }
    res = _compile(plan, ["client", "district"])
    assert isinstance(res, PartialCompile), res
    assert res.miss_parts == [
        {"reason": "extreme_rank_scope_unsupported", "component": "district.A15"}]
    assert "district.A15 = (" not in res.sql   # 谓词放弃
    assert "client.gender = 'F'" in res.sql    # 行级条件照常冻结


def test_cross_table_condition_with_global_scope_keeps_predicate():
    """scope 显式声明全局(0486 的 "among all districts")→ 取未过滤集的极值,谓词成立。"""
    plan = {
        "tables": ["district", "client"],
        "joins": "client.district_id = district.district_id",
        "aggregation": "number of clients",
        "answer_columns": ["number of clients"],
        "conditions": [{"field": "client.gender", "op": "=", "value": "'F'"}],
        "extreme": {"func": "max", "column": "district.A15", "rank": 2,
                    "scope": "second-highest among all districts by A15"},
    }
    res = _compile(plan, ["client", "district"])
    sql = _sql(res)
    assert (
        "district.A15 = (SELECT district.A15 FROM district "
        "ORDER BY district.A15 DESC LIMIT 1 OFFSET 1)"
    ) in sql
    assert "client.gender = 'F'" in sql  # 行级条件仍在(全局只影响子查询取数集)


# ══ 越界形态 → 软 MISS / 跳过(绝不硬拒)══════════════════════


def test_aggregate_form_column_soft_miss():
    """``avg(district.A11)`` 形态:列解析不到声明字段 → 软 MISS
    ``extreme_rank_unsupported``,计划其余组件照常编译。"""
    plan = {
        "tables": ["district"],
        "answer_columns": ["district.A2"],
        "extreme": {"func": "max", "column": "avg(district.A11)", "rank": 2},
    }
    res = _compile(plan, ["district"])
    assert isinstance(res, PartialCompile), res
    assert res.miss_parts == [
        {"reason": "extreme_rank_unsupported", "component": "avg(district.A11)"}]
    assert res.sql == "SELECT district.A2\nFROM district"


def test_unresolvable_column_soft_miss():
    """列不在声明模型内 → 软 MISS ``extreme_rank_unsupported``(不是硬拒)。"""
    plan = {
        "tables": ["district"],
        "answer_columns": ["district.A2"],
        "extreme": {"func": "max", "column": "ghost.col"},
    }
    res = _compile(plan, ["district"])
    assert isinstance(res, PartialCompile), res
    assert res.miss_parts == [
        {"reason": "extreme_rank_unsupported", "component": "ghost.col"}]


def test_non_max_min_func_silently_ignored():
    """func 只认 max/min;其余形态维持旧行为:忽略(不记软 MISS)。"""
    plan = {
        "tables": ["district"],
        "answer_columns": ["district.A2"],
        "extreme": {"func": "avg", "column": "district.A11"},
    }
    res = _compile(plan, ["district"])
    assert isinstance(res, CompileResult), res
    assert res.sql == "SELECT district.A2\nFROM district"


def test_explicit_ordering_skips_extreme():
    """显式 ordering 是更强的呈现声明 → extreme 整体跳过(不叠出矛盾 ORDER BY)。"""
    plan = {
        "tables": ["district"],
        "answer_columns": ["district.A2"],
        "ordering": [{"column": "district.A11", "direction": "desc"}],
        "extreme": {"func": "min", "column": "district.A11"},
    }
    res = _compile(plan, ["district"])
    assert isinstance(res, CompileResult), res
    assert res.sql == (
        "SELECT district.A2\nFROM district\nORDER BY district.A11 DESC")


def test_analysis_present_ignores_extreme_byte_identical():
    """analysis 存在 → extreme 静默忽略(log only,连软 MISS 都不记):
    产物与无 extreme 的同一计划**字节一致**。"""
    base = {
        "tables": ["district"],
        "aggregation": "number of districts",
        "answer_columns": ["number of districts"],
        "analysis": {"type": "share", "metric": "number of districts"},
    }
    with_extreme = {**base, "extreme": {"func": "max", "column": "district.A11",
                                        "rank": 2}}
    a = _compile(base, ["district"])
    b = _compile(with_extreme, ["district"])
    assert not isinstance(a, CompileMiss) and not isinstance(b, CompileMiss)
    assert a.sql == b.sql
    if isinstance(b, PartialCompile):
        assert not any("extreme" in p["reason"] for p in b.miss_parts)


# ══ 投影兜底 / 已表达 ════════════════════════════════════════


def test_extreme_only_plan_falls_back_to_single_agg_projection():
    """只有 extreme、没有投影(0475 型)→ 兜底 ``MAX(极值列)`` 单列,不再
    nothing_compilable 硬 MISS。"""
    plan = {
        "tables": ["district"],
        "extreme": {"func": "max", "column": "district.A11"},
    }
    res = _compile(plan, ["district"])
    assert isinstance(res, CompileResult), res
    assert res.sql == "SELECT MAX(district.A11)\nFROM district"


def test_projection_already_expresses_extreme_no_action():
    """投影已含 ``FUNC(极值列)``(声明度量 max A11)→ 不动:叠加 LIMIT 1 会把
    "每组一条聚合结果"截成一条。rank 也被忽略(分组聚合下的极值显然不适用)。"""
    plan = {
        "tables": ["district"],
        "aggregation": "max A11",
        "answer_columns": ["max A11"],
        "extreme": {"func": "max", "column": "district.A11", "rank": 3},
    }
    res = _compile(plan, ["district"])
    assert isinstance(res, CompileResult), res
    assert res.sql == "SELECT MAX(district.A11)\nFROM district"


def test_extreme_column_pulls_table_into_needed():
    """极值列所在的表自动进 needed(A① 补收)——表格漏列也不产出引用树外表的 SQL。"""
    plan = {
        "tables": ["client"],
        "answer_columns": ["client.client_id"],
        "extreme": {"func": "max", "column": "district.A15"},
    }
    sql = _sql(_compile(plan, ["client", "district"]))
    assert "JOIN district ON client.district_id = district.district_id" in sql
    assert "ORDER BY district.A15 DESC" in sql
