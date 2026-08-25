"""Typed plan AST (PlanQuery) parse tests.

解析语义:顶层形态错误 → None(回退 raw-dict 流);标量强制转换容忍;
语义组件绝不静默丢弃。
"""

from trove.services.semantic_layer.plan import (
    PlanQuery,
    parse_ordering,
    parse_plan_query,
)


def _full_plan() -> dict:
    return {
        "tables": ["loan", "account", "district"],
        "joins": "loan.account_id = account.account_id",
        "conditions": [{"field": "loan.status", "op": "=", "value": "A", "note": "ok"}],
        "aggregation": "count(loan.loan_id)",
        "extreme": {"func": "max", "column": "loan.amount", "scope": "after all filters"},
        "ordering": "district.A3 desc",
        "answer_columns": ["district.A3", "count(loan.loan_id)"],
        "time_grain": {"field": "loan.date", "grain": "month"},
        "having": [{"metric": "total_loan_amount", "op": ">", "value": 10000}],
    }


def test_full_plan_parses():
    q = parse_plan_query(_full_plan())
    assert q is not None
    assert q.tables == ["loan", "account", "district"]
    assert q.aggregation == "count(loan.loan_id)"
    assert q.conditions[0].field == "loan.status"
    assert q.conditions[0].op == "="
    assert q.conditions[0].note == "ok"
    assert q.extreme == {"func": "max", "column": "loan.amount", "scope": "after all filters"}
    assert q.time_grain is not None and q.time_grain.grain == "month"
    assert q.having[0].metric == "total_loan_amount"


def test_to_dict_round_trip():
    q = parse_plan_query(_full_plan())
    assert q is not None
    d = q.to_dict()
    assert d["answer_columns"] == ["district.A3", "count(loan.loan_id)"]
    assert d["conditions"][0]["field"] == "loan.status"
    assert d["time_grain"]["grain"] == "month"
    assert d["having"][0]["op"] == ">"


def test_prose_is_none():
    assert parse_plan_query("no json here") is None


def test_non_dict_is_none():
    assert parse_plan_query(None) is None
    assert parse_plan_query(42) is None


def test_string_condition_item_fails_whole_parse():
    # 静默丢弃过滤条件比整体失败更糟 → 整体 None 回退 dict 流
    plan = {"answer_columns": ["loan.status"], "conditions": ["loan.status = 'A'"]}
    assert parse_plan_query(plan) is None


def test_conditions_not_list_fails():
    assert parse_plan_query({"conditions": "loan.status"}) is None


def test_unknown_grain_fails():
    plan = {"time_grain": {"field": "loan.date", "grain": "fortnight"}}
    assert parse_plan_query(plan) is None


def test_having_needs_exactly_one_of_field_metric():
    assert parse_plan_query({"having": [{"field": "loan.amount", "op": ">", "value": 1}]}) is not None
    assert parse_plan_query({"having": [{"metric": "m1", "op": ">", "value": 1}]}) is not None
    assert parse_plan_query({"having": [{"op": ">", "value": 1}]}) is None
    assert parse_plan_query(
        {"having": [{"field": "a", "metric": "b", "op": ">", "value": 1}]}
    ) is None


def test_ordering_string_form():
    assert parse_ordering("loan.amount desc") == [("loan.amount", "desc")]
    assert parse_ordering("district.A3") == [("district.A3", "asc")]
    assert parse_ordering("a desc, b asc") == [("a", "desc"), ("b", "asc")]
    assert parse_ordering("") == []


def test_ordering_multiword_metric_name():
    # metric 名含空格:"number of loan records desc"
    assert parse_ordering("number of loan records desc") == [
        ("number of loan records", "desc")
    ]


def test_ordering_list_forms():
    assert parse_ordering(["loan.amount desc", "district.A3"]) == [
        ("loan.amount", "desc"),
        ("district.A3", "asc"),
    ]
    assert parse_ordering([{"column": "x", "direction": "descending"}]) == [("x", "desc")]
    assert parse_ordering([]) == []


def test_ordering_invalid_shapes_none():
    assert parse_ordering(123) is None
    assert parse_ordering(["x", 5]) is None
    assert parse_ordering([{"direction": "desc"}]) is None
    assert parse_ordering("   ") == []


def test_aggregation_none_coerces_empty():
    q = parse_plan_query({"answer_columns": ["loan.status"]})
    assert q is not None
    assert q.aggregation == ""
    assert q.to_dict()["aggregation"] == ""


def test_unknown_keys_ignored():
    q = parse_plan_query({"answer_columns": ["loan.status"], "bogus_key": 1})
    assert q is not None
    assert "bogus_key" not in q.to_dict()


def test_plan_query_never_raises_on_garbage():
    # parse_plan_query 永不抛:任意垃圾输入 → None
    for garbage in ([], "x", 3.14, {"conditions": 5}, {"answer_columns": "notalist"},
                    {"ordering": {"column": "x"}}):
        assert parse_plan_query(garbage) is None
