"""grade_complexity 纯函数测试:结构信号(plan) + 语义信号(锚定表)分级。"""

from trove.workflow.complexity import grade_complexity, has_subquery_signal

SIMPLE_PLAN = {
    "tables": ["students"],
    "aggregation": "COUNT",
    "answer_columns": ["count"],
}


def test_none_plan_is_standard():
    """planless(planner 未启用/失败)→ standard:保守,零行为漂移。"""
    assert grade_complexity(None, ["students"]) == "standard"


def test_non_dict_plan_is_standard():
    assert grade_complexity("prose plan text", ["students"]) == "standard"
    assert grade_complexity([], ["students"]) == "standard"


def test_simple_plan():
    assert grade_complexity(SIMPLE_PLAN, ["students"], term_hit=True) == "simple"


def test_simple_needs_semantic_signal():
    """无 term/KB 命中 → standard(语义证据是 simple 的必要条件)。"""
    assert grade_complexity(SIMPLE_PLAN, ["students"]) == "standard"


def test_two_tables_is_simple():
    """≤2 表(允许 join)→ simple;3 表及以上才是 complex。"""
    plan = dict(SIMPLE_PLAN, tables=["students", "counties"])
    assert grade_complexity(plan, ["students", "counties"], term_hit=True) == "simple"


def test_three_tables_is_complex():
    plan = dict(SIMPLE_PLAN, tables=["students", "counties", "teachers"])
    assert grade_complexity(plan, ["students", "counties", "teachers"], term_hit=True) == "complex"


def test_joins_are_simple():
    """≤2 表的 join 不再判 complex(join 文本也不再触发子查询迹象)。"""
    plan = dict(SIMPLE_PLAN, tables=["students", "counties"],
                joins="students.county_id = counties.county_id")
    assert grade_complexity(plan, ["students", "counties"], term_hit=True) == "simple"
    plan2 = dict(SIMPLE_PLAN, tables=["students", "counties"],
                 joins=["students.county_id = counties.county_id"])
    assert grade_complexity(plan2, ["students", "counties"], term_hit=True) == "simple"


def test_subquery_in_conditions_is_complex():
    plan = dict(SIMPLE_PLAN, conditions=[
        {"field": "grade", "op": ">", "value": "SELECT AVG(grade) FROM students"},
    ])
    assert grade_complexity(plan, ["students"], term_hit=True) == "complex"
    assert has_subquery_signal(plan) is True


def test_three_aggregations_is_complex():
    """聚合 ≤ 2 为 simple,≥ 3 才 complex。"""
    plan = dict(SIMPLE_PLAN, aggregation=["COUNT(*)", "AVG(grade)", "SUM(score)"])
    assert grade_complexity(plan, ["students"], term_hit=True) == "complex"


def test_two_aggregations_is_simple():
    plan = dict(SIMPLE_PLAN, aggregation=["COUNT(*)", "AVG(grade)"])
    assert grade_complexity(plan, ["students"], term_hit=True) == "simple"


def test_aggregation_plus_extreme_is_simple():
    """聚合 1 + 极值 1 = 2 → simple。"""
    plan = dict(SIMPLE_PLAN,
                aggregation="COUNT",
                extreme={"func": "MAX", "column": "grade", "scope": "county"})
    assert grade_complexity(plan, ["students"], term_hit=True) == "simple"


def test_aggregation_plus_extreme_plus_agg_is_complex():
    """聚合 2 + 极值 1 = 3 → complex。"""
    plan = dict(SIMPLE_PLAN,
                aggregation=["COUNT(*)", "AVG(grade)"],
                extreme={"func": "MAX", "column": "grade", "scope": "county"})
    assert grade_complexity(plan, ["students"], term_hit=True) == "complex"


def test_plan_validation_dropped_is_complex():
    assert grade_complexity(
        SIMPLE_PLAN, ["students"], term_hit=True,
        plan_validation={"status": "dropped", "errors": ["no such table"]},
    ) == "complex"


def test_ordering_is_allowed_in_simple():
    """排序不再阻塞 simple。"""
    plan = dict(SIMPLE_PLAN, ordering="grade DESC")
    assert grade_complexity(plan, ["students"], term_hit=True) == "simple"


def test_three_answer_columns_are_simple():
    """answer_columns ≤ 3 → simple。"""
    plan = dict(SIMPLE_PLAN, answer_columns=["a", "b", "c"])
    assert grade_complexity(plan, ["students"], term_hit=True) == "simple"


def test_four_answer_columns_make_standard():
    plan = dict(SIMPLE_PLAN, answer_columns=["a", "b", "c", "d"])
    assert grade_complexity(plan, ["students"], term_hit=True) == "standard"


def test_two_matched_tables_are_simple():
    """matched_tables ≤ 2 → simple。"""
    assert grade_complexity(SIMPLE_PLAN, ["students", "counties"], term_hit=True) == "simple"


def test_three_matched_tables_make_standard():
    assert grade_complexity(SIMPLE_PLAN, ["students", "counties", "teachers"], term_hit=True) == "standard"


def test_missing_aggregation_is_simple():
    """无聚合(list 题)也满足「聚合 ≤1」→ simple;multi-candidate 对
    无歧义的单表 list 题同样没有意义。"""
    plan = {"tables": ["students"], "answer_columns": ["name"]}
    assert grade_complexity(plan, ["students"], term_hit=True) == "simple"


def test_wrong_typed_keys_are_conservative():
    """错型键(不可信输入)→ 一律 standard,不崩不冒进。"""
    plan = {"tables": "students", "joins": 42, "aggregation": 3,
            "answer_columns": "name", "ordering": ["x"]}
    assert grade_complexity(plan, ["students"], term_hit=True) == "standard"
    # 单个错型键也足以降级
    assert grade_complexity({"tables": "students"}, ["students"], term_hit=True) == "standard"
    assert grade_complexity({"tables": ["students"], "joins": 42},
                            ["students"], term_hit=True) == "standard"


def test_kb_hit_signal_satisfies_semantic_clause():
    assert grade_complexity(SIMPLE_PLAN, ["students"], kb_hit=True) == "simple"
