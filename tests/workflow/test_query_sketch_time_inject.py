"""Query-sketch time-injection tests: agg_time_dimension 优先于"唯一时间字段"。

parse_date 解析出的时间范围确定性注入 plan.conditions;时间字段选择:
plan 命中的 metric 若声明 agg_time_dimension 则优先(即使 matched 内
多个时间字段也能判定),否则要求唯一时间字段(不猜)。
"""

from __future__ import annotations

from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
)
from trove.workflow.nodes.query_sketch import (
    _inject_time_condition,
    _plan_metric_time_dimension,
)


def _field(name, datatype=None):
    return SemanticField(name=name, expression=name, datatype=datatype)


def _model_with_two_time_fields(metric_name="loan_count", agg_time="loan.date"):
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("date", "Date"),
                _field("updated_at", "DateTime"),
            ]),
        ],
        metrics=[
            SemanticMetric(metric_name, "COUNT(loan.loan_id)",
                           datasets=["loan"], agg_time_dimension=agg_time),
        ],
    )


def test_plan_metric_time_dimension_prefers_declared():
    model = _model_with_two_time_fields()
    plan = {"aggregation": "loan_count", "answer_columns": ["loan_count"]}
    assert _plan_metric_time_dimension(plan, model) == "loan.date"
    # 表达式候选(带括号)按聚合签名匹配到 metric → 同样能定位时间锚点
    assert _plan_metric_time_dimension(
        {"aggregation": "count(loan.loan_id)"}, model) == "loan.date"
    # 未命中任何 metric → None
    assert _plan_metric_time_dimension({"aggregation": "nope"}, model) is None


def test_plan_metric_time_dimension_falls_back_to_dataset_unique_time():
    """metric 未声明 agg_time_dimension 时,回退到其锚定数据集内的唯一
    时间字段(而不是要求 matched 全局唯一)——多表匹配也能注入。"""
    model = SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("date", "Date"),
            ]),
            SemanticDataset(name="account", primary_key=["account_id"], fields=[
                _field("account_id"), _field("date", "Date"),
            ]),
        ],
        metrics=[
            SemanticMetric("loan_count_per_year", "COUNT(loan.loan_id)",
                           datasets=["loan"]),
        ],
    )
    # 多表 matched(loan.date + account.date 两个时间字段)也能锚定 loan.date
    plan = {"aggregation": "count(loan.loan_id)",
            "answer_columns": ["count(loan.loan_id)"]}
    assert _plan_metric_time_dimension(plan, model) == "loan.date"
    fixed = _inject_time_condition(
        dict(plan), "2025-01-01 ~ 2025-01-15", model,
        ["loan", "account"])
    assert fixed is not None
    assert fixed["conditions"][0]["field"] == "loan.date"


def test_inject_time_condition_uses_agg_time_dimension_with_multiple_time_fields():
    """即使 matched 内多时间字段(无唯一),metric 的 agg_time_dimension 也能判定。"""
    model = _model_with_two_time_fields()
    plan = {"aggregation": "loan_count", "answer_columns": ["loan_count"]}
    fixed = _inject_time_condition(plan, "2025-01-01 ~ 2025-01-15", model, ["loan"])
    assert fixed is not None
    conds = fixed["conditions"]
    assert conds[0]["field"] == "loan.date"
    assert conds[0]["op"] == ">=" and conds[0]["value"] == "2025-01-01"
    assert conds[1]["field"] == "loan.date"
    assert conds[1]["op"] == "<=" and conds[1]["value"] == "2025-01-15"
    assert fixed["plan_field"] == "inject_time_condition"


def test_inject_time_condition_without_declared_time_still_unique_only():
    """metric 未声明 agg_time_dimension 时仍走"唯一时间字段"规则(不猜)。"""
    model = _model_with_two_time_fields(agg_time="")
    plan = {"aggregation": "loan_count", "answer_columns": ["loan_count"]}
    fixed = _inject_time_condition(plan, "2025-01-01 ~ 2025-01-15", model, ["loan"])
    assert fixed is None  # 多时间字段且无声明 → 不注入

    single = SemanticModel(
        name="fin",
        datasets=[SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
            _field("loan_id"), _field("date", "Date"),
        ])],
        metrics=[SemanticMetric("loan_count", "COUNT(loan.loan_id)",
                                datasets=["loan"])],
    )
    fixed = _inject_time_condition(plan, "2025-01-01 ~ 2025-01-15", single, ["loan"])
    assert fixed is not None
    assert fixed["conditions"][0]["field"] == "loan.date"


def test_inject_time_condition_skips_when_field_already_conditioned():
    model = _model_with_two_time_fields()
    plan = {
        "aggregation": "loan_count",
        "answer_columns": ["loan_count"],
        "conditions": [{"field": "loan.date", "op": ">=", "value": "2024-01-01"}],
    }
    fixed = _inject_time_condition(plan, "2025-01-01 ~ 2025-01-15", model, ["loan"])
    assert fixed is None  # 已有该字段条件,不重复注入
