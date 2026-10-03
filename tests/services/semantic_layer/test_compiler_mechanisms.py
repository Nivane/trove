"""Lane A(语义编译器机制)单文件回归:每项机制都带**反向**测试。

覆盖以下各组:

- **A1.1 扇出提升**:``COUNT(<1 端>.pk)`` → ``COUNT(DISTINCT …)``(行计数语义);
  多端/非主键/``COUNT(*)``/M:N 边一律不动(反向四例)。
- **A1.2 签名 PK 容忍**:``COUNT(pk)`` ≡ ``COUNT(DISTINCT pk)`` 双向;非主键
  列的 DISTINCT 仍严格不等;裸列主键判定要求唯一命中。
- **A3a/A3b 条件所有权**:``and`` 复合伪条件不进骨架口径(条件重排不再误判
  「丢过滤」);软 MISS 聚合候选**内部**谓词不进骨架 WHERE(孪生行级条件
  归聚合所有),值不同的条件照常冻结。
- **A3c 骨架降级(advisory)**:计划级缺口(声明聚合未落地 / analysis 未解析)
  才降级 WHERE 校验;外围缺口不降级;RLS ``row_filters`` 恒校验。
- **A1d 维度侧 having 折叠**:单组 + 1 端维度列的单列 AVG/MIN/MAX → 行级
  WHERE;分组/事实侧/SUM/COUNT 三例反向都保持 HAVING。
- **A4b 值路由**:字面量唯一命中别处值词表 → 重锚该字段并接上声明连边;
  无显式 joins/源有词表/多命中/无唯一连接/数字字面量/非等值算子 一律不动。
- **A5a 表对修复**:显式 joins 列名写错 → 按表对上的声明唯一路径修复,
  基数守卫不豁免;并覆盖 needed 表。
- **B3 排序第三档**:排序引用 answer 列的聚合候选 → ``ORDER BY <内联表达式>``。

模型一律合成(不依赖 BIRD gold);断言只钉形状与语义,不钉数据值。
"""

from __future__ import annotations

import pytest

from trove.services.semantic_layer.compiler import (
    CompileMiss,
    PartialCompile,
    SemanticCompiler,
    _skeleton_where,
    skeleton_preserved,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)


def _field(name, **kw):
    return SemanticField(name=name, expression=kw.pop("expr", name), **kw)


def _fin_model():
    """client(多)──district(一);district.A2 具名词表、A5 数字串词表。"""
    district = SemanticDataset(name="district", primary_key=["district_id"], fields=[
        _field("district_id"), _field("A2"), _field("A11"), _field("region_id"),
    ])
    district.fields[0].values = []          # 主键探测过但无词表(防御)
    district.fields[1].values = ["Sokolov", "Prague"]
    district.fields[2].values = ["5", "101", "0"]   # 数字串:不入值索引
    client = SemanticDataset(name="client", primary_key=["client_id"], fields=[
        _field("client_id"), _field("district_id"), _field("gender"),
        _field("income"),
    ])
    return SemanticModel(
        name="fin",
        datasets=[client, district],
        relationships=[
            SemanticRelationship("client_to_district", "client", "district",
                                 from_columns=["district_id"],
                                 to_columns=["district_id"], cardinality="1:N"),
        ],
        metrics=[
            SemanticMetric("number of districts", "COUNT(district.district_id)",
                           datasets=["district"]),
            SemanticMetric("number of clients", "COUNT(client.client_id)",
                           datasets=["client"]),
            SemanticMetric("sum A11", "SUM(district.A11)", datasets=["district"]),
            SemanticMetric("count A11", "COUNT(district.A11)", datasets=["district"]),
            SemanticMetric("avg A11", "AVG(district.A11)", datasets=["district"]),
            SemanticMetric("avg income", "AVG(client.income)", datasets=["client"]),
        ],
    )


def _compile(plan, matched, model=None, dialect="mysql"):
    return SemanticCompiler(model or _fin_model()).compile_detailed(
        plan, list(matched), force_dialect=dialect)


def _sql(result):
    assert not isinstance(result, CompileMiss), f"unexpected MISS: {result}"
    return result.sql


def _miss(result):
    assert isinstance(result, CompileMiss), f"expected MISS, got: {result}"
    return result.reason


# ══ A1.1 扇出提升 ═════════════════════════════════════════════


def test_fanout_count_promoted_on_one_end():
    """0470 全形状:「1」端维度表的行计数在 1:N 联路径上补 DISTINCT。"""
    plan = {
        "tables": ["district", "client"],
        "aggregation": "number of districts",
        "answer_columns": ["number of districts"],
        "conditions": [{"field": "client.gender", "op": "=", "value": "F"}],
    }
    sql = _sql(_compile(plan, ["client", "district"]))
    assert sql == (
        "SELECT COUNT(DISTINCT district.district_id)\n"
        "FROM district\n"
        "JOIN client ON client.district_id = district.district_id\n"
        "WHERE client.gender = 'F'"
    )


def test_fanout_count_promoted_client_shape_0495():
    """0495 形状:client 在「1」端(FK 在 loan 侧)→ COUNT(client.client_id) 补 DISTINCT。

    与 0470 同机制、不同实体(被联的维度是 client 而非 district):提升
    判据是「列所在表在联路径的 1 端 + 列是声明主键」,与表名无关。
    """
    model = SemanticModel(
        name="loan_fin",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("client_id"), _field("amount")]),
            SemanticDataset(name="client", primary_key=["client_id"], fields=[
                _field("client_id"), _field("name")]),
        ],
        relationships=[SemanticRelationship(
            "loan_to_client", "loan", "client",
            from_columns=["client_id"], to_columns=["client_id"],
            cardinality="1:N")],
        metrics=[
            SemanticMetric("number of clients", "COUNT(client.client_id)",
                           datasets=["client"]),
            SemanticMetric("total loan", "SUM(loan.amount)", datasets=["loan"]),
        ],
    )
    plan = {
        "tables": ["client", "loan"],
        "aggregation": "number of clients",
        "answer_columns": ["number of clients"],
        "conditions": [{"field": "loan.amount", "op": ">", "value": 1000}],
    }
    sql = _sql(_compile(plan, ["client", "loan"], model=model))
    assert sql == (
        "SELECT COUNT(DISTINCT client.client_id)\n"
        "FROM client\n"
        "JOIN loan ON loan.client_id = client.client_id\n"
        "WHERE loan.amount > 1000"
    )


def test_fanout_count_not_promoted_on_many_end():
    """多端的行计数不加 DISTINCT(每行本就出现一次;加了反而错)。"""
    plan = {
        "tables": ["client", "district"],
        "aggregation": "number of clients",
        "answer_columns": ["number of clients"],
    }
    sql = _sql(_compile(plan, ["client", "district"]))
    assert "COUNT(client.client_id)" in sql
    assert "DISTINCT" not in sql


def test_fanout_count_not_promoted_for_non_pk_column():
    """非主键列的 COUNT 不改:DISTINCT 非键列是**另一个度量**。"""
    plan = {
        "tables": ["district", "client"],
        "aggregation": "count A11",
        "answer_columns": ["count A11"],
    }
    sql = _sql(_compile(plan, ["client", "district"]))
    assert "COUNT(district.A11)" in sql
    assert "DISTINCT" not in sql


def test_fanout_count_star_not_promoted():
    """``COUNT(*)`` 无列身份,不参与提升。"""
    model = _fin_model()
    model.metrics.append(
        SemanticMetric("rows", "COUNT(*)", datasets=["district"]))
    plan = {
        "tables": ["district", "client"],
        "aggregation": "rows",
        "answer_columns": ["rows"],
    }
    sql = _sql(_compile(plan, ["client", "district"], model=model))
    assert "COUNT(*)" in sql
    assert "DISTINCT" not in sql


def test_fanout_count_not_promoted_on_mn_edge():
    """M:N(dedup 豁免)边不进 one_end:去重只保证单侧,行对数仍非行计数。"""
    model = _fin_model()
    model.relationships[0] = SemanticRelationship(
        "client_to_district", "client", "district",
        from_columns=["district_id"], to_columns=["district_id"],
        cardinality="M:N", fan_out="dedup")
    plan = {
        "tables": ["district", "client"],
        "aggregation": "number of districts",
        "answer_columns": ["number of districts"],
    }
    sql = _sql(_compile(plan, ["client", "district"], model=model))
    assert "COUNT(district.district_id)" in sql
    assert "DISTINCT" not in sql


# ══ A1.2 签名 PK 容忍 ═════════════════════════════════════════


def test_pk_distinct_tolerance_forward():
    """候选 ``COUNT(DISTINCT pk)`` 命中声明 ``COUNT(pk)``(键唯一 → 等价)。"""
    plan = {
        "tables": ["district", "client"],
        "aggregation": "COUNT(DISTINCT district.district_id)",
        "answer_columns": ["COUNT(DISTINCT district.district_id)"],
        "conditions": [{"field": "client.gender", "op": "=", "value": "F"}],
    }
    sql = _sql(_compile(plan, ["client", "district"]))
    # 命中的是**声明的**度量(表达式权威),提升再补上 DISTINCT
    assert "COUNT(DISTINCT district.district_id)" in sql
    assert "number of districts" not in sql


def test_pk_distinct_tolerance_reverse():
    """声明 ``COUNT(DISTINCT pk)``、候选裸 ``COUNT(pk)`` → 互认。"""
    model = _fin_model()
    model.metrics[0] = SemanticMetric(
        "number of districts", "COUNT(DISTINCT district.district_id)",
        datasets=["district"])
    plan = {
        "tables": ["district", "client"],
        "aggregation": "COUNT(district.district_id)",
        "answer_columns": ["COUNT(district.district_id)"],
    }
    sql = _sql(_compile(plan, ["client", "district"], model=model))
    assert "COUNT(DISTINCT district.district_id)" in sql


def test_pk_tolerance_requires_unambiguous_bare_column():
    """裸列 PK 判定要求**唯一**命中数据集;两个数据集同名主键 → 不放宽。"""
    model = _fin_model()
    # 另加一张以 client_id 为主键的快照表:裸列 client_id 的 PK 判定即二义。
    model.datasets.append(SemanticDataset(
        name="client_history", primary_key=["client_id"],
        fields=[_field("client_id"), _field("snapshot_day")]))
    model.metrics.append(
        SemanticMetric("client rows", "COUNT(client_id)", datasets=["client"]))
    plan = {
        "tables": ["client"],
        "aggregation": "COUNT(DISTINCT client_id)",
        "answer_columns": ["COUNT(DISTINCT client_id)"],
    }
    assert _miss(_compile(plan, ["client"], model=model)) == "no_metric_match"


# ══ A3a 条件抽取口径(and 复合跳过、or 保留)════════════════════


def test_skeleton_where_skips_and_composites():
    """``A AND B`` 只收叶子 —— 复合伪条件(列集并集 + and 算子)不存在。"""
    from sqlglot import parse_one

    tree = parse_one("SELECT 1 FROM t WHERE a = 1 AND b = 2")
    conds = _skeleton_where(tree)
    assert conds == {
        (frozenset({("", "a")}), "eq", ("1",)),
        (frozenset({("", "b")}), "eq", ("2",)),
    }
    assert all(op != "and" for _cols, op, _vals in conds)


def test_skeleton_where_keeps_or_composite():
    """``or`` 是整体逻辑条件(叶子不分别成立)→ 保留整块。"""
    from sqlglot import parse_one

    tree = parse_one("SELECT 1 FROM t WHERE (a = 1 OR b = 2) AND c = 3")
    conds = _skeleton_where(tree)
    ors = [(cols, vals) for cols, op, vals in conds if op == "or"]
    assert len(ors) == 1, conds
    cols, _vals = ors[0]
    assert {c[1] for c in cols} == {"a", "b"}
    assert (frozenset({("", "c")}), "eq", ("3",)) in conds
    assert all(op != "and" for _cols, op, _vals in conds)


def test_condition_reorder_is_not_a_dropped_filter():
    """条件重排(A AND B → B AND A)不再被误判为「丢了过滤条件」;丢一个仍拦。"""
    from sqlglot import parse_one

    from trove.services.semantic_layer.contract import PlanContract

    skeleton = "SELECT COUNT(*) FROM t WHERE a = 1 AND b = 2"
    contract = PlanContract(
        skeleton_sql=skeleton,
        where=_skeleton_where(parse_one(skeleton)),
    )
    ok, why = skeleton_preserved(
        contract, "SELECT COUNT(*) FROM t WHERE b = 2 AND a = 1", "sqlite")
    assert ok, why
    dropped, why2 = skeleton_preserved(
        contract, "SELECT COUNT(*) FROM t WHERE b = 2", "sqlite")
    assert not dropped and "filter" in why2


# ══ A3b 软 MISS 聚合候选的条件所有权 ═══════════════════════════

_SHARE_CAND = (
    "COUNT(CASE WHEN client.gender = 'F' AND client.district_id = 'Sokolov' "
    "THEN client.client_id END)"
)


def test_soft_agg_candidate_conditions_not_frozen_into_skeleton():
    """候选内部的分子条件不冻结进骨架 WHERE(否则生成侧重写必被保真打回)。"""
    plan = {
        "tables": ["district", "client"],
        "answer_columns": ["district.A2", _SHARE_CAND],
        "aggregation": _SHARE_CAND,
        "conditions": [
            {"field": "client.gender", "op": "=", "value": "'F'"},
            {"field": "district.A2", "op": "=", "value": "'Prague'"},
        ],
    }
    result = _compile(plan, ["client", "district"])
    assert isinstance(result, PartialCompile)
    where_cols = {c for cols, _op, _vals in result.contract.where for c in cols}
    assert ("client", "gender") not in where_cols      # 归聚合所有
    assert ("district", "a2") in where_cols            # 普通行级条件照常
    assert "client.gender = 'F'" not in result.sql


def test_condition_with_other_value_still_frozen():
    """反向:值不同(非候选内部谓词)→ 照常冻结进骨架。"""
    plan = {
        "tables": ["district", "client"],
        "answer_columns": ["district.A2", _SHARE_CAND],
        "aggregation": _SHARE_CAND,
        "conditions": [{"field": "client.gender", "op": "=", "value": "'M'"}],
    }
    result = _compile(plan, ["client", "district"])
    assert isinstance(result, PartialCompile)
    where_cols = {c for cols, _op, _vals in result.contract.where for c in cols}
    assert ("client", "gender") in where_cols


# ══ A3c 骨架降级(advisory)与 RLS 硬底线 ═══════════════════════


def _advisory_plan():
    """agg_declared 但聚合文本对不上任何度量 → 计划级缺口。"""
    return {
        "tables": ["district", "client"],
        "aggregation": "share of female clients",
        "answer_columns": ["district.A2", "share of female clients"],
        "conditions": [
            {"field": "district.A11", "op": ">", "value": 5000},
            {"field": "client.gender", "op": "=", "value": "F"},
        ],
    }


def test_plan_level_gap_marks_skeleton_advisory():
    result = _compile(_advisory_plan(), ["client", "district"])
    assert isinstance(result, PartialCompile)
    assert result.contract.advisory is True
    # WHERE 被生成侧重写(丢掉骨架里的过滤)→ advisory 下放行;join 保留
    regenerated = (
        "SELECT district.A2, COUNT(CASE WHEN client.gender = 'F' "
        "THEN client.client_id END)\n"
        "FROM district\n"
        "JOIN client ON client.district_id = district.district_id"
    )
    ok, why = skeleton_preserved(result.contract, regenerated, "mysql")
    assert ok, why


def test_advisory_still_enforces_joins():
    """降级只放 WHERE;join ⊇ 照旧(结构骨架仍权威)。"""
    result = _compile(_advisory_plan(), ["client", "district"])
    assert isinstance(result, PartialCompile)
    no_join = "SELECT district.A2 FROM district"
    ok, why = skeleton_preserved(result.contract, no_join, "mysql")
    assert not ok and "join" in why


def test_analysis_gap_marks_skeleton_advisory_and_keeps_group_width():
    plan = _advisory_plan()
    plan["aggregation"] = "number of districts"
    plan["answer_columns"] = ["district.A2", "number of districts"]
    plan["analysis"] = {"type": "share", "order_by": "district.nonexistent"}
    result = _compile(plan, ["client", "district"])
    assert isinstance(result, PartialCompile)
    assert result.contract.advisory is True
    assert any(g["reason"].startswith("analysis_") for g in result.miss_parts)
    # 分组宽度不随 advisory 降级(join 保留、WHERE 归 advisory 豁免)
    dropped = (
        "SELECT district.A2, COUNT(district.district_id)\n"
        "FROM district\n"
        "JOIN client ON client.district_id = district.district_id\n"
        "WHERE district.A11 > 5000 AND client.gender = 'F'"
    )
    ok, why = skeleton_preserved(result.contract, dropped, "mysql")
    assert not ok and "grouping" in why


def test_peripheral_gap_stays_hard():
    """外围缺口(单条条件字段解析失败)不降级:已解析出的 WHERE 仍权威。"""
    plan = _advisory_plan()
    plan["aggregation"] = "number of districts"
    plan["answer_columns"] = ["district.A2", "number of districts"]
    plan["conditions"] = [
        {"field": "district.nonexistent", "op": "=", "value": "x"},
        {"field": "district.A11", "op": ">", "value": 5000},
    ]
    result = _compile(plan, ["client", "district"])
    assert isinstance(result, PartialCompile)
    assert result.contract.advisory is False
    dropped = (
        "SELECT district.A2, COUNT(DISTINCT district.district_id)\n"
        "FROM district\n"
        "JOIN client ON client.district_id = district.district_id"
    )
    ok, why = skeleton_preserved(result.contract, dropped, "mysql")
    assert not ok and "filter" in why


def test_advisory_still_enforces_row_filters():
    """RLS 是声明层授权,不随 advisory 降级:丢了照打回。"""
    model = _fin_model()
    model.datasets[1].row_filter = "region_id = 'R1'"   # district.region_id
    result = _compile(_advisory_plan(), ["client", "district"], model=model)
    assert isinstance(result, PartialCompile)
    assert result.contract.advisory is True
    assert result.contract.row_filters, "RLS 谓词必须进契约"
    kept = (
        "SELECT district.A2\n"
        "FROM district\n"
        "JOIN client ON client.district_id = district.district_id\n"
        "WHERE district.A11 > 5000 AND client.gender = 'F' "
        "AND district.region_id = 'R1'"
    )
    ok, why = skeleton_preserved(result.contract, kept, "mysql")
    assert ok, why
    dropped = (
        "SELECT district.A2\n"
        "FROM district\n"
        "JOIN client ON client.district_id = district.district_id"
    )
    ok2, why2 = skeleton_preserved(result.contract, dropped, "mysql")
    assert not ok2 and "row filter" in why2


def test_share_without_dimension_is_soft_miss_not_constant_window():
    """单投影 share 会退化成恒 1.0 窗口 → 软 MISS 回退内层聚合,不产包装。"""
    plan = {
        "tables": ["district", "client"],
        "aggregation": "number of districts",
        "answer_columns": ["number of districts"],
        "analysis": {"type": "share", "metric": "number of districts"},
    }
    result = _compile(plan, ["client", "district"])
    assert isinstance(result, PartialCompile)
    assert "OVER" not in result.sql
    assert any(g["reason"] == "analysis_invalid" for g in result.miss_parts)


# ══ A1d 维度侧 having 折叠 ═════════════════════════════════════


def test_dimension_side_avg_having_folds_into_where():
    """0470 形状:单组 + 1 端维度列 AVG → 行级 WHERE(而非恒真 HAVING)。"""
    plan = {
        "tables": ["district", "client"],
        "aggregation": "number of districts",
        "answer_columns": ["number of districts"],
        "conditions": [{"field": "client.gender", "op": "=", "value": "'F'"}],
        "having": [
            {"metric": "avg(district.A11)", "op": ">", "value": 6000},
            {"metric": "avg(district.A11)", "op": "<", "value": 10000},
        ],
    }
    sql = _sql(_compile(plan, ["client", "district"]))
    assert sql == (
        "SELECT COUNT(DISTINCT district.district_id)\n"
        "FROM district\n"
        "JOIN client ON client.district_id = district.district_id\n"
        "WHERE client.gender = 'F' AND district.A11 > 6000 "
        "AND district.A11 < 10000"
    )
    assert "HAVING" not in sql


def test_grouped_having_stays_having():
    """反向:有 GROUP BY → 每组的 AVG 不是行值,保持 HAVING。"""
    plan = {
        "tables": ["district", "client"],
        "aggregation": "number of districts",
        "answer_columns": ["client.gender", "number of districts"],
        "having": [{"metric": "avg(district.A11)", "op": ">", "value": 6000}],
    }
    sql = _sql(_compile(plan, ["client", "district"]))
    assert "GROUP BY" in sql
    assert "HAVING AVG(district.A11) > 6000" in sql
    assert "district.A11 > 6000" not in sql


def test_fact_side_avg_having_stays_having():
    """反向:事实侧(多端)列的 AVG 行级化会换语义 → 保持 HAVING。"""
    plan = {
        "tables": ["district", "client"],
        "aggregation": "number of districts",
        "answer_columns": ["number of districts"],
        "conditions": [{"field": "client.gender", "op": "=", "value": "F"}],
        "having": [{"metric": "avg income", "op": ">", "value": 6000}],
    }
    sql = _sql(_compile(plan, ["client", "district"]))
    assert "HAVING AVG(client.income) > 6000" in sql
    assert "client.income > 6000" not in sql


@pytest.mark.parametrize("metric", ["sum A11", "count A11"])
def test_dimension_side_sum_count_having_stays_having(metric):
    """反向:SUM/COUNT 是跨行聚集,维度行级化语义不同 → 保持 HAVING。"""
    plan = {
        "tables": ["district", "client"],
        "aggregation": "number of districts",
        "answer_columns": ["number of districts"],
        "conditions": [{"field": "client.gender", "op": "=", "value": "F"}],
        "having": [{"metric": metric, "op": ">", "value": 6000}],
    }
    sql = _sql(_compile(plan, ["client", "district"]))
    assert "HAVING" in sql
    assert "WHERE client.gender = 'F'" in sql
    assert "district.A11 > 6000" not in sql


def test_having_metric_expression_ref_resolves():
    """having.metric 写表达式形态(非度量名)也能对账到声明度量并折叠。"""
    plan = {
        "tables": ["district", "client"],
        "aggregation": "number of districts",
        "answer_columns": ["number of districts"],
        "conditions": [{"field": "client.gender", "op": "=", "value": "F"}],
        "having": [{"metric": "AVG(district.A11)", "op": ">", "value": 6000}],
    }
    sql = _sql(_compile(plan, ["client", "district"]))
    assert "district.A11 > 6000" in sql
    assert "HAVING" not in sql


def test_having_unknown_expression_ref_is_soft_miss():
    """反向:对不上任何声明度量的表达式引用 → 软 MISS,不猜。"""
    plan = {
        "tables": ["district", "client"],
        "aggregation": "number of districts",
        "answer_columns": ["number of districts"],
        "having": [{"metric": "STDDEV(district.A11)", "op": ">", "value": 1}],
    }
    result = _compile(plan, ["client", "district"])
    assert isinstance(result, PartialCompile)
    assert any(g["reason"] == "having_metric_unknown" for g in result.miss_parts)
    assert "STDDEV" not in result.sql


# ══ A4b 值路由 ════════════════════════════════════════════════


def _reroute_plan(conditions, joins):
    return {
        "tables": ["client", "district"],
        "joins": joins,
        "aggregation": "number of clients",
        "answer_columns": ["number of clients"],
        "conditions": conditions,
    }


_REROUTE_JOINS = "client.district_id = district.district_id"


def test_value_reroute_moves_condition_to_owning_field():
    """区名写在 client.district_id 上 → 重锚 district.A2 并接上声明连边。"""
    plan = _reroute_plan(
        [{"field": "client.district_id", "op": "=", "value": "'Sokolov'"}],
        _REROUTE_JOINS)
    sql = _sql(_compile(plan, ["client", "district"]))
    assert "district.A2 = 'Sokolov'" in sql
    assert "client.district_id = 'Sokolov'" not in sql
    assert "JOIN district ON client.district_id = district.district_id" in sql


def test_value_reroute_appends_missing_join_edge():
    """连边不在 plan.joins 里 → 追加(否则重锚后的表不可达 → 硬 MISS)。"""
    model = _fin_model()
    model.datasets.append(SemanticDataset(
        name="region", primary_key=["region_id"],
        fields=[_field("region_id"), _field("name")]))
    model.relationships.append(SemanticRelationship(
        "district_to_region", "district", "region",
        from_columns=["region_id"], to_columns=["region_id"], cardinality="1:N"))
    plan = {
        "tables": ["client", "district", "region"],
        "joins": "district.region_id = region.region_id",   # 缺 client—district
        "aggregation": "number of clients",
        "answer_columns": ["region.name", "number of clients"],
        "conditions": [{"field": "client.district_id", "op": "=", "value": "'Sokolov'"}],
    }
    sql = _sql(_compile(plan, ["client", "district", "region"], model=model))
    assert "district.A2 = 'Sokolov'" in sql
    assert "JOIN district ON client.district_id = district.district_id" in sql
    assert "JOIN region ON district.region_id = region.region_id" in sql
    assert sql.index("JOIN district") < sql.index("JOIN region")   # 左深树序


def test_value_reroute_skipped_without_explicit_joins():
    """无显式 joins → 不重锚(BFS 通道的选边不可审计,保持今日行为)。"""
    plan = _reroute_plan(
        [{"field": "client.district_id", "op": "=", "value": "'Sokolov'"}], "")
    sql = _sql(_compile(plan, ["client", "district"]))
    assert "client.district_id = 'Sokolov'" in sql
    assert "district.A2" not in sql


def test_value_reroute_skipped_when_source_has_vocabulary():
    """源字段自带值词表 → 值归它,索引无权改判(哪怕字面量别处唯一命中)。"""
    model = _fin_model()
    model.datasets[1].fields[1].values.append("Ostrava")   # district.A2 再收一个
    model.datasets[0].fields[2].values = ["F", "M"]        # client.gender 有词表
    plan = _reroute_plan(
        [{"field": "client.gender", "op": "=", "value": "Ostrava"}],
        _REROUTE_JOINS)
    sql = _sql(_compile(plan, ["client", "district"], model=model))
    assert "client.gender = 'Ostrava'" in sql
    assert "district.A2" not in sql


def test_value_reroute_skipped_on_multiple_hits():
    """字面量命中多个字段(跨数据集)→ 二义,不重锚。"""
    model = _fin_model()
    model.datasets[1].fields[3].values = ["Sokolov"]      # district.region_id 也收
    plan = _reroute_plan(
        [{"field": "client.district_id", "op": "=", "value": "'Sokolov'"}],
        _REROUTE_JOINS)
    sql = _sql(_compile(plan, ["client", "district"], model=model))
    assert "client.district_id = 'Sokolov'" in sql


def test_value_reroute_skipped_when_no_unique_connection():
    """目标数据集与源之间没有声明关系 → 不重锚(接了也是错边)。"""
    model = _fin_model()
    model.datasets.append(SemanticDataset(
        name="region", primary_key=["region_id"],
        fields=[_field("region_id"), _field("name")]))
    model.datasets[2].fields[1].values = ["Ostrava"]
    plan = _reroute_plan(
        [{"field": "client.district_id", "op": "=", "value": "'Ostrava'"}],
        _REROUTE_JOINS)
    sql = _sql(_compile(plan, ["client", "district"], model=model))
    assert "client.district_id = 'Ostrava'" in sql
    assert "region" not in sql


def test_value_reroute_skipped_when_two_relationships_between_pair():
    """源→目标表对有多条声明关系(表对一义、走法二义)→ 不重锚。"""
    model = _fin_model()
    model.relationships.append(SemanticRelationship(
        "client_to_district_alt", "client", "district",
        from_columns=["district_id"], to_columns=["district_id"],
        cardinality="1:N"))
    plan = _reroute_plan(
        [{"field": "client.district_id", "op": "=", "value": "'Sokolov'"}],
        _REROUTE_JOINS)
    sql = _sql(_compile(plan, ["client", "district"], model=model))
    assert "client.district_id = 'Sokolov'" in sql
    assert "district.A2" not in sql


def test_value_reroute_skipped_for_numeric_literal():
    """数字字面量不是值词表查询(数字串列不入索引)→ 绝不重锚。

    反向测试(协调方指定的数值危险面):district.A5 是**数字串**列(如
    "5" / "101"),若进了值索引,``client.district_id = '5'`` 会被唯一命中
    并重锚成 A5 的计数过滤 —— 静默错误。数字字面量在索引构建期就被剔除。
    """
    model = _fin_model()
    a5 = model.datasets[1].fields[2]
    assert "5" in a5.values   # 前提:若 A5 入了索引,'5' 确实会唯一命中它
    plan = _reroute_plan(
        [{"field": "client.district_id", "op": "=", "value": "'5'"}],
        _REROUTE_JOINS)
    sql = _sql(_compile(plan, ["client", "district"], model=model))
    assert "client.district_id = '5'" in sql
    assert "district.A5" not in sql
    # 索引里确实没有数字串(构建期剔除,而不是靠用时的字面量形态兜底)
    assert all(not key.isdigit() for key in SemanticCompiler(model)._value_index())


def test_value_reroute_skipped_for_non_equality_op():
    """非等值算子(= / in 之外)的值是区间/模式语义 → 不重锚。"""
    plan = _reroute_plan(
        [{"field": "client.district_id", "op": ">", "value": "'Sokolov'"}],
        _REROUTE_JOINS)
    sql = _sql(_compile(plan, ["client", "district"]))
    assert "client.district_id > 'Sokolov'" in sql


def test_value_reroute_handles_in_list():
    """``in`` 列表:全部字面量唯一命中同一目标 → 整条重锚(值列表原样保留)。"""
    plan = _reroute_plan(
        [{"field": "client.district_id", "op": "in",
          "value": "('Sokolov', 'Prague')"}],
        _REROUTE_JOINS)
    sql = _sql(_compile(plan, ["client", "district"]))
    assert "district.A2 IN ('Sokolov', 'Prague')" in sql
    assert "client.district_id IN" not in sql


# ══ A5a 表对修复 ══════════════════════════════════════════════


def _chain_model(*, account_cardinality="1:N", to_column="district_id",
                 loan_cardinality="1:N", non_unique_to: bool = False):
    """loan ── account ── district 链(声明 1:N),修复杂交用。

    ``non_unique_to=True``:loan→account 的 to 侧落在非唯一列 ``branch_id``
    上;配合 ``loan_cardinality=""`` 即「基数未声明且无从推断」。
    """
    loan_to = ["account_id"]
    to = ["branch_id"] if non_unique_to else ["account_id"]
    account_fields = [_field("account_id"), _field("district_id")]
    if non_unique_to:
        account_fields.append(_field("branch_id"))
    return SemanticModel(
        name="chain",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("account_id"), _field("amount"),
            ]),
            SemanticDataset(name="account", primary_key=["account_id"],
                            fields=account_fields),
            SemanticDataset(name="district", primary_key=["district_id"], fields=[
                _field("district_id"), _field("A2"),
            ]),
        ],
        relationships=[
            SemanticRelationship("loan_to_account", "loan", "account",
                                 from_columns=loan_to,
                                 to_columns=to, cardinality=loan_cardinality),
            SemanticRelationship("account_to_district", "account", "district",
                                 from_columns=["district_id"],
                                 to_columns=[to_column],
                                 cardinality=account_cardinality),
        ],
        metrics=[SemanticMetric("total_loan", "SUM(loan.amount)", datasets=["loan"])],
    )


def test_explicit_repair_extends_tree_to_needed_tables():
    """修复出的边集不含投影引用的表 → 按声明路径补(不是丢给 unreachable)。"""
    plan = {
        "tables": ["loan", "account", "district"],
        "joins": "loan.loan_id = account.account_id",
        "aggregation": "total_loan",
        "answer_columns": ["district.A2", "total_loan"],
    }
    sql = _sql(_compile(plan, ["loan", "account", "district"], model=_chain_model()))
    assert "JOIN account ON loan.account_id = account.account_id" in sql
    assert "JOIN district ON account.district_id = district.district_id" in sql


def test_explicit_repair_does_not_bypass_cardinality_guard():
    """修复走同一套显式 channel:修复出的边上基数无从判定 → 仍严格 MISS。"""
    plan = {
        "tables": ["loan", "account"],
        "joins": "loan.loan_id = account.account_id",
        "aggregation": "total_loan",
        "answer_columns": ["total_loan"],
    }
    # 修复出的边 to 侧是非唯一列(branch_id)且基数未声明 → 无从判定 → MISS
    model = _chain_model(loan_cardinality="", non_unique_to=True)
    assert _miss(_compile(plan, ["loan", "account"], model=model)) == "unknown_cardinality"
    # 对照:唯一键支撑(to 侧=account 主键)时同一修复路径放行
    ok_model = _chain_model(loan_cardinality="", non_unique_to=False)
    assert not isinstance(_compile(plan, ["loan", "account"], model=ok_model), CompileMiss)


def test_explicit_repair_mn_path_still_rejected():
    """修复出的路径含 M:N 边 → 与显式声明同判:fan_out 严格 MISS。"""
    plan = {
        "tables": ["loan", "account"],
        "joins": "loan.loan_id = account.account_id",
        "aggregation": "total_loan",
        "answer_columns": ["total_loan"],
    }
    model = _chain_model()
    model.relationships[0] = SemanticRelationship(
        "loan_to_account", "loan", "account",
        from_columns=["account_id"], to_columns=["account_id"],
        cardinality="M:N")
    assert _miss(_compile(plan, ["loan", "account"], model=model)) == "fan_out"


# ══ B3 排序第三档 ═════════════════════════════════════════════


def test_ordering_by_answer_aggregate_candidate():
    """排序列复述 answer_columns 的聚合表达式 → ORDER BY 内联度量表达式。"""
    plan = {
        "tables": ["district", "client"],
        "aggregation": "COUNT(DISTINCT client.client_id)",
        "answer_columns": ["district.A2", "COUNT(DISTINCT client.client_id)"],
        "ordering": [{"column": "count(distinct  client.client_id)",
                      "direction": "desc"}],
    }
    sql = _sql(_compile(plan, ["client", "district"]))
    assert "ORDER BY COUNT(client.client_id) DESC" in sql


def test_ordering_by_unknown_column_still_dropped():
    """反向:对不上任何度量/字段的排序键 → 丢弃(宽处理),不 MISS。"""
    plan = {
        "tables": ["district", "client"],
        "aggregation": "number of districts",
        "answer_columns": ["district.A2", "number of districts"],
        "ordering": [{"column": "who.knows", "direction": "asc"}],
    }
    sql = _sql(_compile(plan, ["client", "district"]))
    assert "ORDER BY" not in sql
