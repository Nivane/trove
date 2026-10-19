"""缺口归一:类别闭集、草稿规格预览、深链、按组汇总。

草稿能力必须**等于** ``candidate_specs`` 的判断 —— 这里不测「它想不想」,
只测它没有在别处另发明一套「可草稿」口径。
"""

from __future__ import annotations

from types import SimpleNamespace

from trove.services.question_coverage.gaps import (
    GAP_KINDS,
    aggregate_gaps,
    build_gaps,
    kind_for,
    make_gap,
    synthetic_dataset_gap,
    workbench_link,
)

DS = "financial"


def _model(*, region_declared: bool = False):
    fields = [
        SimpleNamespace(
            name="amount", values=[], value_aliases={},
            expression="loan.amount",
        ),
        SimpleNamespace(
            name="status", values=["A", "B", "C"], value_aliases={},
            expression="loan.status",
        ),
    ]
    if region_declared:
        fields.append(SimpleNamespace(
            name="region", values=[], value_aliases={},
            expression="loan.region",
        ))
    loan = SimpleNamespace(
        name="loan", description="Loan record",
        fields=fields, primary_key=["loan_id"], entity=None,
    )
    return SimpleNamespace(datasets=[loan], metrics=[])


# ── 分类闭集 ────────────────────────────────────────────────────────────


def test_kind_for_covers_every_reason_and_defaults_to_other():
    expected = {
        "no_metric_match": "metric",
        "having_metric_unknown": "metric",
        "metric_anchor_unmatched": "metric",
        "unresolved_filter_field": "field",
        "unresolved_answer_column": "field",
        "time_field_not_declared": "field",
        "analysis_partition_unresolved": "field",
        "analysis_order_unresolved": "field",
        "missing_filter_value": "value",
        "enum_value_unresolved": "value",
        "unknown_cardinality": "relation",
        "fan_out": "relation",
        "ambiguous_join_path": "relation",
        "unreachable_table": "relation",
        "no_semantic_match": "dataset",
        "no_semantic_layer": "dataset",
        "no_model": "dataset",
        "entity_naming": "entity",
    }
    for reason, kind in expected.items():
        assert kind_for(reason) == kind
        assert kind in GAP_KINDS
    assert kind_for("something_new") == "other"
    assert kind_for("") == "other"


def test_workbench_links_point_only_at_real_landing_sites():
    link = workbench_link("relation", DS, "loan, account")
    assert link == "/admin/semantic?ds=financial&tab=datasets&q=loan"
    assert workbench_link("dataset", DS) == "/admin/kb?ds=financial"
    assert workbench_link("metric", DS, "loan.amount") == ""
    assert workbench_link("relation", "", "loan") == ""


# ── 单缺口:规格预览 ────────────────────────────────────────────────────


def test_metric_gap_is_draftable_from_an_aggregate_expression():
    gap = make_gap(
        "no_metric_match", "SUM(loan.amount)", model=_model(), datasource=DS)
    assert gap["kind"] == "metric"
    assert gap["draftable"] is True
    assert gap["specs"][0]["kind"] == "metric"
    assert gap["specs"][0]["name"] == "sum_amount"
    assert gap["label"]["zh"] == "指标"


def test_field_gap_also_yields_the_entity_preview():
    gap = make_gap(
        "unresolved_filter_field", "loan.region",
        model=_model(), datasource=DS)
    assert gap["kind"] == "field"
    assert gap["draftable"] is True
    kinds = [s["kind"] for s in gap["specs"]]
    assert kinds == ["field", "entity"]
    assert gap["specs"][0]["name"] == "loan.region"


def test_value_gap_requires_the_third_key_and_a_probed_value():
    model = _model()
    hit = make_gap(
        "enum_value_unresolved", "loan.status", "A",
        model=model, datasource=DS)
    assert hit["draftable"] is True
    assert hit["specs"][0]["payload"]["value_aliases"] == {"A": ["A"]}

    # 两键老 miss(值缺席)→ 不猜(与 candidate_specs 同一条证据门)。
    miss_two_key = make_gap(
        "enum_value_unresolved", "loan.status", None,
        model=model, datasource=DS)
    assert miss_two_key["draftable"] is False
    assert miss_two_key["specs"] == []


def test_already_declared_field_is_not_a_draft_candidate():
    gap = make_gap(
        "unresolved_filter_field", "loan.region",
        model=_model(region_declared=True), datasource=DS)
    # 字段已声明、数据集有 primary_key → 只剩实体命名候选(若未声明实体)。
    assert all(s["kind"] != "field" for s in gap["specs"])


def test_relation_gap_is_not_draftable_and_carries_a_workbench_link():
    gap = make_gap(
        "unknown_cardinality", "loan, account",
        model=_model(), datasource=DS)
    assert gap["kind"] == "relation"
    assert gap["draftable"] is False
    assert gap["workbench"].startswith("/admin/semantic?ds=financial")
    assert "unknown_cardinality" not in str(gap["label"])


def test_partial_three_key_misses_keep_their_value():
    gaps = build_gaps(
        [{"reason": "enum_value_unresolved", "component": "loan.status",
          "value": "A"}],
        model=_model(), datasource=DS)
    assert gaps[0]["value"] == "A"
    assert gaps[0]["draftable"] is True


def test_duplicate_misses_collapse_inside_one_question():
    gaps = build_gaps(
        [
            {"reason": "no_metric_match", "component": "SUM(loan.amount)"},
            {"reason": "no_metric_match", "component": "SUM(loan.amount)"},
        ],
        model=_model(), datasource=DS)
    assert len(gaps) == 1


def test_synthetic_dataset_gap_never_pretends_to_have_a_component():
    gap = synthetic_dataset_gap("no_model", datasource=DS)
    assert gap["kind"] == "dataset"
    assert gap["component"] == ""
    assert gap["workbench"] == "/admin/kb?ds=financial"


# ── 汇总:定序、样例、按当前模型重算 ─────────────────────────────────────


def _results():
    field_gap = make_gap(
        "unresolved_filter_field", "loan.region",
        model=_model(), datasource=DS)
    relation_gap = make_gap(
        "unknown_cardinality", "loan, account",
        model=_model(), datasource=DS)
    return [
        {"seq": 2, "question": "q2", "gaps": [relation_gap]},
        {"seq": 0, "question": "q0", "gaps": [field_gap, relation_gap]},
        {"seq": 1, "question": "q1", "gaps": [field_gap]},
        {"seq": 3, "question": "q0", "gaps": []},
    ]


def test_aggregate_counts_samples_and_samples_are_deduped():
    groups = aggregate_gaps(
        _results(), model=_model(), datasource=DS)
    by_reason = {g["reason"]: g for g in groups}
    field = by_reason["unresolved_filter_field"]
    assert field["count"] == 2
    assert field["samples"] == ["q0", "q1"]
    assert field["seqs"] == [0, 1]
    relation = by_reason["unknown_cardinality"]
    assert relation["count"] == 2
    assert relation["draftable"] is False
    # 定序:count 降序,同 count 按 kind/reason/component。
    assert [g["count"] for g in groups] == [2, 2]
    assert groups[0]["kind"] == "field"


def test_aggregate_recomputes_draftability_with_the_current_model():
    # run 时的预览说可草稿;读取时该字段已被声明 → 汇总必须改口。
    groups = aggregate_gaps(
        _results(), model=_model(region_declared=True), datasource=DS)
    field = next(
        g for g in groups if g["reason"] == "unresolved_filter_field")
    assert all(s["kind"] != "field" for s in field["specs"])


def test_aggregate_without_model_is_never_draftable():
    groups = aggregate_gaps(_results(), model=None, datasource=DS)
    assert groups and all(not g["draftable"] for g in groups)
