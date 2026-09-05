"""时间轴空档补全(time_spine):模型声明 + 时间范围 → spine LEFT JOIN。

模型声明 ``time_spine`` 且查询带时间分桶 + 可从过滤条件推导时间范围时,
编译器把聚合结果 LEFT JOIN 到按 grain 生成的密集周期表,缺期按 fill 补全。
范围不可推/无声明 → 常规路径(无填充,行为不变)。
"""
from trove.services.semantic_layer.compiler import (
    CompileMiss,
    SemanticCompiler,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    TimeSpine,
)


def _field(name, datatype=None):
    return SemanticField(name=name, expression=name, datatype=datatype)


def _spine_model(fill="0"):
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("account_id"), _field("amount"),
                _field("date", "Date"),
            ]),
        ],
        metrics=[
            SemanticMetric("number of loan records", "COUNT(loan.loan_id)",
                           datasets=["loan"]),
        ],
        time_spine=TimeSpine(field="loan.date", granularity="month", fill=fill),
    )


def _compile(plan, model=None):
    return SemanticCompiler(model or _spine_model()).compile_detailed(
        plan, ["loan"])


def _time_plan(bounds):
    return {
        "aggregation": "count(loan.loan_id)",
        "answer_columns": ["count(loan.loan_id)"],
        "time_grain": {"field": "loan.date", "grain": "month"},
        "conditions": [
            {"field": "loan.date", "op": ">=", "value": bounds[0]},
            {"field": "loan.date", "op": "<=", "value": bounds[1]},
        ],
    }


def test_spine_fill_zero():
    result = _compile(_time_plan(("1994-01-01", "1994-03-31")))
    assert not isinstance(result, CompileMiss)
    sql = result.sql
    assert "spine.period" in sql
    assert "LEFT JOIN" in sql
    assert "COALESCE(t._c1, 0)" in sql
    # 90 天 → 89 步递归
    assert "SELECT 0 UNION ALL SELECT n + 1 FROM _seq WHERE n < 89" in sql
    assert "ORDER BY spine.period" in sql


def test_spine_year_bound():
    """YYYY 条件值 → 整年跨度。"""
    result = _compile(_time_plan(("1994", "1996")))
    assert not isinstance(result, CompileMiss)
    sql = result.sql
    # 1994-01-01 .. 1996-12-31 = 1096 天
    assert "WHERE n < 1095" in sql


def test_spine_fill_previous():
    result = _compile(_time_plan(("1994-01-01", "1994-02-28")),
                      model=_spine_model(fill="previous"))
    assert not isinstance(result, CompileMiss)
    assert "LAG(t._c1) OVER (ORDER BY spine.period)" in result.sql


def test_spine_fill_none_no_coalesce():
    result = _compile(_time_plan(("1994-01-01", "1994-02-28")),
                      model=_spine_model(fill="none"))
    assert not isinstance(result, CompileMiss)
    assert "COALESCE" not in result.sql


def test_spine_without_bounds_falls_back():
    """无时间条件 → 无 spine(常规路径,不填充)。"""
    plan = {
        "aggregation": "count(loan.loan_id)",
        "answer_columns": ["count(loan.loan_id)"],
        "time_grain": {"field": "loan.date", "grain": "month"},
    }
    result = _compile(plan)
    assert not isinstance(result, CompileMiss)
    assert "LEFT JOIN" not in result.sql
    assert "spine.period" not in result.sql


def test_spine_requires_model_declaration():
    """模型未声明 time_spine → 常规路径。"""
    model = _spine_model()
    model.time_spine = None
    result = _compile(_time_plan(("1994-01-01", "1994-03-31")), model=model)
    assert not isinstance(result, CompileMiss)
    assert "LEFT JOIN" not in result.sql


def test_spine_skipped_with_analysis():
    """analysis 存在 → 不叠加 spine(窗口包装优先)。"""
    plan = _time_plan(("1994-01-01", "1994-03-31"))
    plan["analysis"] = {"type": "running_total"}
    result = _compile(plan)
    assert not isinstance(result, CompileMiss)
    assert "spine.period" not in result.sql
