"""Typed plan AST (PlanQuery) parse tests.

解析语义:顶层形态错误 → None(回退 raw-dict 流);标量强制转换容忍;
语义组件绝不静默丢弃。
"""

from trove.services.semantic_layer.plan import (
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


def test_ordering_direction_suffix_wins_over_field():
    """B3:方向词被吞进 column 的后缀时,**后缀为准**(0483 实形状)。

    模型把方向词写进列名("count(account.account_id) 降序")、而 direction
    字段填了矛盾值("asc")——它表达的方向在文本里,字段值只是位置填错。
    """
    assert parse_ordering([{
        "column": "count(account.account_id) 降序", "direction": "asc",
    }]) == [("count(account.account_id)", "desc")]
    assert parse_ordering([{
        "column": "count(account.account_id) desc", "direction": "asc",
    }]) == [("count(account.account_id)", "desc")]
    # 后缀 asc 对抗字段 desc:同样以后缀为准
    assert parse_ordering([{
        "column": "count(account.account_id) ascending", "direction": "desc",
    }]) == [("count(account.account_id)", "asc")]
    # 无后缀 → 照旧读字段
    assert parse_ordering([{"column": "x", "direction": "desc"}]) == [("x", "desc")]


def test_ordering_direction_suffix_order_tail():
    """``desc order`` / ``升序排列前的方向词``:末 token 是 order 时先摘掉再判。"""
    assert parse_ordering("loan.amount desc order") == [("loan.amount", "desc")]
    assert parse_ordering([{"column": "loan.amount desc order"}]) == [
        ("loan.amount", "desc")
    ]
    # 多词列名 + 后缀:只切方向词,列名其余部分原样保留
    assert parse_ordering("number of loan records 降序") == [
        ("number of loan records", "desc")
    ]


def test_ordering_suffix_not_confused_by_column_names():
    """子串不算方向词:列名含 "desc"(description)不切坏。"""
    assert parse_ordering("description") == [("description", "asc")]
    assert parse_ordering([{"column": "description", "direction": "desc"}]) == [
        ("description", "desc")
    ]


def test_ordering_bare_direction_word_is_invalid():
    """整段就是方向词(没有列名)→ 无效段 None,不当列名容忍。

    容忍成 ("desc", "asc") 会生成 ``ORDER BY desc`` —— 不是合法列引用,
    失败被推迟到编译期、换成更难读的错;判 None 则走既有失败路径
    (limit 在场时正是 limit_without_order 重规划反馈要接的形态)。
    """
    assert parse_ordering("desc") is None
    assert parse_ordering("asc") is None
    assert parse_ordering(" 降序 ") is None
    assert parse_ordering("desc order") is None
    assert parse_ordering([{"column": "desc", "direction": "asc"}]) is None
    # 逗号列表里混入无效段 → 整条失败(与其余非法段同一方向)
    assert parse_ordering("a desc, desc") is None


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


def test_render_plan_time_grain_and_having_lines():
    from trove.workflow.nodes.query_sketch import _render_plan

    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["loan.date", "sum(loan.amount)"],
        "time_grain": {"field": "loan.date", "grain": "month"},
        "having": [{"metric": "total_loan_amount", "op": ">", "value": 10000}],
    }
    en = _render_plan(plan, "en")
    assert "Time grain: loan.date by month" in en
    assert "Having:" in en
    assert "- total_loan_amount > 10000" in en
    zh = _render_plan(plan, "zh")
    assert "时间粒度: loan.date 按月" in zh
    assert "聚合后过滤:" in zh


def test_analysis_and_limit_parsed():
    q = parse_plan_query({
        "answer_columns": ["loan.date", "sum(loan.amount)"],
        "time_grain": {"field": "loan.date", "grain": "month"},
        "analysis": {"type": "mom", "metric": "total_loan_amount", "order_by": "loan.date"},
        "limit": 5,
    })
    assert q is not None
    assert q.analysis is not None
    assert q.analysis.type == "mom"
    assert q.analysis.metric == "total_loan_amount"
    assert q.analysis.order_by == "loan.date"
    assert q.limit == 5
    dumped = q.to_dict()
    assert dumped["analysis"]["type"] == "mom"
    assert dumped["limit"] == 5


def test_analysis_missing_defaults_none():
    q = parse_plan_query({"answer_columns": ["loan.date"]})
    assert q is not None and q.analysis is None and q.limit is None


def test_analysis_unknown_type_fails_plan():
    assert parse_plan_query({"analysis": {"type": "pivot"}}) is None


def test_analysis_wrong_shape_fails_plan():
    assert parse_plan_query({"analysis": "notadict"}) is None
    assert parse_plan_query({"analysis": {"type": "share", "partition_by": "x"}}) is None


def test_limit_invalid_fails_plan():
    assert parse_plan_query({"limit": "abc"}) is None
    assert parse_plan_query({"limit": -3}) is not None  # 负数宽松置空


def test_render_plan_analysis_line():
    from trove.workflow.nodes.query_sketch import _render_plan

    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["district.A3", "sum(loan.amount)"],
        "analysis": {"type": "share", "metric": "total_loan_amount"},
        "limit": 10,
    }
    en = _render_plan(plan, "en")
    assert "Analysis: share" in en
    assert "Limit: 10" in en
    zh = _render_plan(plan, "zh")
    assert "分析: share" in zh
    assert "限量: 10" in zh
