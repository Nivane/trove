"""口径测试 —— 结局闭集、分类优先级、指标分母、diff 方向。

这一组是体检的**尺子本身**:任何一条先红,后面的引擎/服务/前端都不该被相信。
"""

from __future__ import annotations

from trove.services.question_coverage import taxonomy as T


# ── 闭集 ────────────────────────────────────────────────────────────────


def test_outcome_sets_partition_the_closed_set():
    # 十个结局,两两不交,三块合起来恰好是全集 —— 没有「哪一类都不算」的缝。
    assert len(T.OUTCOMES) == 10
    assert len(set(T.OUTCOMES)) == 10
    assert T.EVALUATED_OUTCOMES | T.UNMEASURED_OUTCOMES == frozenset(T.OUTCOMES)
    assert not (T.EVALUATED_OUTCOMES & T.UNMEASURED_OUTCOMES)
    assert T.EVALUATED_OUTCOMES | T.UNMEASURED_OUTCOMES == set(T.OUTCOMES)


def test_coverage_rank_covers_exactly_the_evaluated_outcomes():
    # 覆盖序只给「可判」的结局排序;不可判的四类是 not_comparable,不是最差。
    assert set(T.COVERAGE_RANK) == set(T.EVALUATED_OUTCOMES)
    assert T.COVERAGE_RANK["certified"] > T.COVERAGE_RANK["compiled"]
    assert T.COVERAGE_RANK["compiled"] > T.COVERAGE_RANK["partial"]
    assert T.COVERAGE_RANK["partial"] > T.COVERAGE_RANK["soft_miss"]
    assert T.COVERAGE_RANK["soft_miss"] > T.COVERAGE_RANK["refused"]
    assert T.COVERAGE_RANK["refused"] > T.COVERAGE_RANK["unmatched"]


# ── classify:优先级逐分支 ────────────────────────────────────────────────


def _c(**kw):
    base = dict(
        intent="query",
        fast_path=False,
        no_model=False,
        refusal_reason="",
        compile_meta=None,
        plan_validation_status="",
        last_call_error=False,
        llm_calls=1,
    )
    base.update(kw)
    return T.classify(**base)


def test_non_query_intent_is_not_query_before_anything_else():
    # 意图门在最前:即便同时带着 compile_meta,也不是 coverage 的判断对象。
    assert _c(intent="metadata", compile_meta={"outcome": "compiled"}) == (
        "not_query", "metadata")
    assert _c(intent="chitchat") == ("not_query", "chitchat")
    assert _c(intent="") == ("not_query", "non_query")
    for intent in ("write", "correction", "confirm"):
        assert _c(intent=intent)[0] == "not_query"


def test_attribution_intent_continues():
    assert _c(intent="attribution", fast_path=True) == ("certified", "")


def test_certified_beats_missing_compile_meta():
    # 快径命中时根本没有 compile_meta;判 certified 不能先看 meta。
    assert _c(fast_path=True) == ("certified", "")


def test_unmatched_for_no_model_and_no_semantic_match():
    assert _c(no_model=True) == ("unmatched", "no_model")
    assert _c(refusal_reason="no_semantic_match") == (
        "unmatched", "no_semantic_match")
    # no_model 优先于空 meta 的 error 分支 —— 零锚定不是失败。
    assert _c(no_model=True, llm_calls=0) == ("unmatched", "no_model")


def test_plan_validation_dropped_is_unplanned_not_error():
    outcome, reason = _c(plan_validation_status="dropped", llm_calls=1)
    assert (outcome, reason) == ("unplanned", "plan_validation_dropped")


def test_llm_error_vs_empty_plan_distinguished_by_counter():
    # 最后一次调用失败(JSON 回退也无格式重试仍失败)→ error
    assert _c(last_call_error=True, llm_calls=1) == ("error", "llm_error")
    # 调用过且成功,但计划为空 → unplanned(不是 LLM 的错)
    assert _c(last_call_error=False, llm_calls=1) == ("unplanned", "empty_plan")
    # 一次都没调用 = 节点在 LLM 之前异常 → error
    assert _c(last_call_error=False, llm_calls=0) == (
        "error", "sketch_failed_before_llm")


def test_no_semantic_layer_short_circuit_is_unmatched():
    meta = {"outcome": "miss", "miss_reason": "no_semantic_layer", "miss_class": "hard"}
    assert _c(compile_meta=meta, refusal_reason="refuse") == (
        "unmatched", "no_semantic_layer")


def test_compiled_and_partial():
    assert _c(compile_meta={"outcome": "compiled"}) == ("compiled", "")
    outcome, reason = _c(compile_meta={
        "outcome": "partial",
        "partial_reasons": ["enum_value_unresolved", "no_metric_match"],
    })
    assert outcome == "partial"
    assert reason == "enum_value_unresolved,no_metric_match"


def test_soft_miss_differs_from_refused():
    soft = {"outcome": "miss", "miss_class": "soft", "miss_reason": "enum_value_unresolved"}
    assert _c(compile_meta=soft) == ("soft_miss", "enum_value_unresolved")
    # 生产源码(``query_sketch.py`` 的 ``_plan_has_intent and (miss is None or
    # is_hard_miss)`` 分支)只对硬 MISS 置 refusal,软 MISS 从不拒绝;分类器
    # 先判 soft 是防御排序 —— 真出现这个不可能的组合,也按软缺口读,不虚报拒绝。
    assert _c(compile_meta=soft, refusal_reason="compile_miss") == (
        "soft_miss", "enum_value_unresolved")


def test_hard_miss_refused_carries_miss_reason():
    hard = {"outcome": "miss", "miss_class": "hard", "miss_reason": "fan_out"}
    assert _c(compile_meta=hard, refusal_reason="compile_miss") == ("refused", "fan_out")
    # 拒绝原因退化为 refusal 形状时也不丢
    no_reason = {"outcome": "miss", "miss_class": "hard"}
    assert _c(compile_meta=no_reason, refusal_reason="refuse") == ("refused", "refuse")


def test_hard_miss_without_refusal_signal_is_unplanned():
    # 无拒绝信号的 miss = 计划没表达出可编译意图;prose 与 typed 分因。
    hard = {"outcome": "miss", "miss_class": "hard", "miss_reason": "x"}
    assert _c(compile_meta=hard) == ("unplanned", "prose_fallback")
    typed = dict(hard, plan_typed=True)
    assert _c(compile_meta=typed) == ("unplanned", "no_plan_intent")


# ── ratio / aggregate ───────────────────────────────────────────────────


def test_ratio_zero_denominator_is_none_not_zero():
    assert T.ratio(0, 0) == {"num": 0, "den": 0, "rate": None}
    assert T.ratio(3, 0)["rate"] is None
    assert T.ratio(1, 3)["rate"] == round(1 / 3, 4)
    assert T.ratio(2, 2)["rate"] == 1.0


def _row(outcome: str, reason: str = "", miss_class: str = "", miss_reason: str = ""):
    return {
        "outcome": outcome,
        "outcome_reason": reason,
        "miss_class": miss_class,
        "miss_reason": miss_reason,
    }


def test_aggregate_metrics_and_denominator():
    results = [
        _row("certified"),
        _row("compiled"),
        _row("partial", miss_reason="enum_value_unresolved"),
        _row("soft_miss", miss_class="soft", miss_reason="enum_value_unresolved"),
        _row("refused", miss_class="hard", miss_reason="fan_out"),
        _row("unmatched", miss_reason="no_model"),
        _row("not_query", "chitchat"),
        _row("unplanned", "empty_plan"),
    ]
    s = T.aggregate(results)
    assert s["question_total"] == 8
    assert s["evaluated"] == 6
    assert s["unmeasured"] == 2
    # 可确定性 = (certified+compiled)/evaluated = 2/6
    assert s["metrics"]["deterministic_coverage"]["num"] == 2
    assert s["metrics"]["deterministic_coverage"]["den"] == 6
    # 受治理 = 3/6
    assert s["metrics"]["governed_coverage"]["num"] == 3
    # 缺口 = soft_miss + refused + unmatched = 3/6
    assert s["metrics"]["gap_rate"]["num"] == 3
    assert s["metrics"]["refusal_rate"]["num"] == 1
    assert s["metrics"]["unmatched_rate"]["num"] == 1
    assert s["metrics"]["passthrough_rate"]["num"] == 1
    assert s["by_miss_class"] == {"hard": 1, "soft": 1}
    # 断点原因聚合按 count desc,再按 reason 字典序
    assert s["by_miss_reason"] == [
        {"reason": "enum_value_unresolved", "count": 2},
        {"reason": "fan_out", "count": 1},
        {"reason": "no_model", "count": 1},
    ]
    assert T.HIGH_UNMEASURED_RATIO == 0.20
    assert "high_unmeasured" not in s["warnings"]


def test_aggregate_all_not_query_has_no_rate():
    s = T.aggregate([_row("not_query", "chitchat")] * 3)
    assert s["evaluated"] == 0
    assert s["metrics"]["deterministic_coverage"] == {"num": 0, "den": 0, "rate": None}
    assert s["metrics"]["gap_rate"]["rate"] is None


def test_aggregate_unknown_outcome_folds_to_error():
    # 未知结局绝不进 counts 的新键;归为 error 且不进口径。
    s = T.aggregate([_row("bogus")])
    assert s["counts"]["error"] == 1
    assert "bogus" not in s["counts"]
    assert s["evaluated"] == 0


def test_aggregate_high_unmeasured_warning():
    results = [_row("error")] * 3 + [_row("compiled")] * 2
    s = T.aggregate(results)
    assert "high_unmeasured" in s["warnings"]


def test_aggregate_by_outcome_is_in_closed_set_order():
    s = T.aggregate([_row("unmatched"), _row("certified")])
    assert [r["outcome"] for r in s["by_outcome"]] == list(T.OUTCOMES)
    assert s["by_outcome"][0] == {
        "outcome": "certified", "count": 1, "evaluated": True}
    idx = {r["outcome"]: r["evaluated"] for r in s["by_outcome"]}
    assert idx["error"] is False


# ── diff ────────────────────────────────────────────────────────────────


def _run(*rows):
    return {"results": [
        {"question": q, "outcome": o, "outcome_reason": r} for q, o, r in rows
    ]}


def test_diff_direction_and_matching():
    base = _run(
        ("q1", "refused", "fan_out"),
        ("q2", "compiled", ""),
        ("q3", "error", "llm_error"),
        ("q4", "partial", "a"),
    )
    current = _run(
        ("q1", "compiled", ""),       # improved
        ("q2", "refused", "fan_out"),  # regressed
        ("q3", "error", "llm_error"),  # not_comparable
        ("q4", "partial", "a"),        # same
        ("q5", "compiled", ""),        # current_only
    )
    d = T.diff(base, current)
    assert d["matched"] == 4
    assert d["base_only"] == 0
    assert d["current_only"] == 1
    by_q = {row["question"]: row for row in d["questions"]}
    assert by_q["q1"]["change"] == "improved"
    assert by_q["q2"]["change"] == "regressed"
    assert by_q["q3"]["change"] == "not_comparable"
    assert by_q["q4"]["change"] == "same"
    assert d["changes"] == {
        "improved": 1, "regressed": 1, "changed": 0, "same": 1, "not_comparable": 1,
    }


def test_diff_same_outcome_different_reason_is_changed():
    base = _run(("q", "refused", "fan_out"))
    current = _run(("q", "refused", "unknown_cardinality"))
    d = T.diff(base, current)
    assert d["changes"]["changed"] == 1
    assert d["questions"][0]["change"] == "changed"


def test_diff_matches_normalized_text_and_duplicate_occurrence():
    base = _run(
        ("Which region?", "compiled", ""),
        ("which   region?  ", "refused", "fan_out"),  # 归一后同名,出现序号 2
    )
    current = _run(
        ("WHICH REGION?", "compiled", ""),
        ("which region?", "refused", "fan_out"),
    )
    d = T.diff(base, current)
    assert d["matched"] == 2
    assert [row["change"] for row in d["questions"]] == ["same", "same"]
    # 重复问题的配对是「第 1 个对第 1 个」,不是按文本整体对齐
    assert [row["base_seq"] for row in d["questions"]] == [0, 1]


def test_diff_questions_sorted_by_current_seq():
    base = _run(("a", "compiled", ""), ("b", "compiled", ""))
    current = _run(("b", "compiled", ""), ("a", "compiled", ""))
    d = T.diff(base, current)
    assert [row["question"] for row in d["questions"]] == ["b", "a"]
    assert [row["seq"] for row in d["questions"]] == [0, 1]


def test_summary_delta_none_safe():
    base = {"metrics": {"deterministic_coverage": {"num": 1, "den": 2, "rate": 0.5}}}
    current = {"metrics": {"deterministic_coverage": {"num": 2, "den": 2, "rate": 1.0}}}
    d = T.summary_delta(base, current)
    assert d["deterministic_coverage"]["delta"] == 0.5
    # 任一侧缺席 → delta=None,不把缺席算成 0
    d2 = T.summary_delta({}, current)
    assert d2["deterministic_coverage"]["delta"] is None
    assert d2["deterministic_coverage"]["base"] == {}
