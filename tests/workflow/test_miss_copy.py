"""miss_copy —— 编译 MISS 文案的唯一源。

三个出口(拒绝节点、拒绝报表、覆盖率体检)共用这一份;这里钉的是**结构性
不变量**,不是逐字快照:带 ``{c}`` 的模板必须有通用标签兜底,任何出口都
不该把 ``{c}`` 或内部 slug 摆给用户。
"""

from __future__ import annotations

from trove.workflow import miss_copy as M


def test_every_placeholder_template_has_a_generic_label():
    # label 没有行级 component 可插值 —— 带 {c} 的模板若缺通用说法,界面就会
    # 原样显示 "{c}"。这条断言把两处常量绑在一起(新增带 {c} 的 reason 必须
    # 同时补 _GENERIC_COPY)。
    for reason, (zh, en) in M.MISS_COPY.items():
        if "{c}" in zh or "{c}" in en:
            label = M.miss_label(reason)
            assert "{c}" not in label["zh"], reason
            assert "{c}" not in label["en"], reason


def test_labels_never_leak_slug_or_placeholder():
    for reason in M.MISS_COPY:
        label = M.miss_label(reason)
        assert label["zh"] and label["en"]
        assert "{c}" not in label["zh"] and "{c}" not in label["en"]
        # 已知 slug 的标签不是 slug 本身(泛化说法可以相同,但不会是 slug 原文)
        assert label["zh"] != reason
    unknown = M.miss_label("some_future_reason")
    assert unknown["zh"] != "some_future_reason"


def test_miss_phrase_interpolates_only_where_declared():
    assert M.miss_phrase("zh", "no_metric_match", "loan.amount") == "指标(如 loan.amount)"
    assert M.miss_phrase("en", "fan_out", "a, b") == (
        "the aggregation path between the tables (a, b) (a plain join double-counts)")
    # ambiguous_join_path 的 component 可能是内部诊断串 —— 模板刻意为空插值位
    phrase = M.miss_phrase("zh", "ambiguous_join_path", "explicit joins ...")
    assert "explicit joins" not in phrase
    # 未知 reason → 泛化说法,不露 slug
    assert M.miss_phrase("zh", "brand_new_reason") == "概念(指标、字段或筛选口径)"


def test_relation_reasons_order_is_pinned():
    # 拒绝报表的 reasons 响应顺序由这个 tuple 决定;换成 set 会让同一份数据
    # 两次读出不同字节。
    assert M.RELATION_MISS_REASONS == (
        "unknown_cardinality", "fan_out", "ambiguous_join_path", "unreachable_table",
    )
    assert M.TABLE_COPY_REASONS == frozenset({"unknown_cardinality", "fan_out"})
    # 只有这两个 reason 的文案模板带 {c} —— 插值边界与显示名替换同源
    for reason, (zh, en) in M.MISS_COPY.items():
        has_ph = "{c}" in zh or "{c}" in en
        assert has_ph == (reason in ("no_metric_match", "having_metric_unknown",
                                     "unknown_cardinality", "fan_out")), reason
