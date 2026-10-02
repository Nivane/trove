"""草稿校验 / lint 结构化 —— 语义工作台 P2 地基的纯函数层。

三条不变量(比「字段对不对」重要):

1. **结构化映射不丢问题**:未归类的串落 ``code="lint"``,绝不静默消失;
2. **validate 与 confirm 同判**:凡是 confirm 会拒的,validate 必须 ok=False
   (反之亦然)—— 方法就是走同一条 ``_apply_draft`` + 同一份文档 lint;
3. **干跑零副作用**:validate / diff 都不改输入文档。
"""

from __future__ import annotations

import copy

import pytest

from trove.services.semantic_layer.issues import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    structured_issues,
    validate_draft,
)
from trove.services.semantic_layer.manage import _apply_draft, _draft_diff

DOC = {
    "version": "0.2.0.dev0",
    "semantic_model": [{
        "name": "test_db",
        "datasets": [
            {"name": "students", "source": "students",
             "primary_key": ["id"],
             "fields": [
                 {"name": "id", "datatype": "Integer",
                  "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                               "expression": "id"}]}},
                 {"name": "grade", "datatype": "Integer",
                  "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                               "expression": "grade"}]}},
                 {"name": "enrolled_at", "datatype": "Date",
                  "dimension": {"is_time": True},
                  "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                               "expression": "enrolled_at"}]}},
             ]},
            {"name": "courses", "source": "courses",
             "fields": [
                 {"name": "course_id", "datatype": "Integer",
                  "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                               "expression": "course_id"}]}},
             ]},
        ],
        "metrics": [{
            "name": "平均成绩",
            "description": "学生平均分",
            "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                         "expression": "AVG(students.grade)"}]},
            "filter": "students.grade > 0",
            "agg_time_dimension": "students.enrolled_at",
        }],
    }],
}


# ── structured_issues ─────────────────────────────────


def test_structured_issues_maps_known_families():
    items = structured_issues([
        "指标「平均成绩」重复定义",
        "关系「loan_to_account」未声明基数(cardinality),编译器将保守 MISS",
        "表 students 字段「grade」重复定义",
        "表 students.grade 含空/非法 synonym",
        "文档顶层的 masking 会被忽略 —— 请写在 semantic_model[0] 下(与 time_spine 同层)",
    ])
    assert [i["code"] for i in items] == [
        "dup_metric", "relationship_missing_cardinality", "dup_field",
        "bad_synonym", "masking_misplaced",
    ]
    assert [i["severity"] for i in items] == [
        SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_ERROR,
        SEVERITY_WARNING, SEVERITY_ERROR,
    ]
    assert items[0]["target"] == {"kind": "metric", "name": "平均成绩"}
    assert items[1]["target"]["kind"] == "relationship"
    assert items[2]["target"] == {"kind": "field", "name": "students.grade"}
    assert items[4]["target"] == {"kind": "document", "name": ""}
    # hint 不是空串:每条都要给前端一句话修复建议
    assert all(i["hint"] for i in items)


def test_structured_issues_write_path_errors_get_targets():
    """写盘路径 ValueError 的文本也走同一张规则表(validate 干跑复用)。"""
    items = structured_issues([
        "指标「x」锚定的数据集未声明: courses(修正数据集名...)",
        "数据集不存在: nope",
        "字段表达式必填",
    ])
    assert [i["code"] for i in items] == [
        "dataset_undeclared", "dataset_unknown", "expr_missing"]
    assert items[0]["target"] == {"kind": "metric", "name": "x"}
    assert items[1]["target"] == {"kind": "dataset", "name": "nope"}


def test_structured_issues_never_drops_unclassified():
    """未归类串兜底为 code=lint(warning)—— 映射失败不该让真问题消失。"""
    items = structured_issues(["半句没见过的问题描述"])
    assert items[0]["code"] == "lint"
    assert items[0]["severity"] == SEVERITY_WARNING
    assert items[0]["target"] == {"kind": "unknown", "name": ""}
    assert items[0]["message"] == "半句没见过的问题描述"


def test_structured_issues_strips_sqlglot_ansi():
    """sqlglot 的解析错误带 ANSI 转义,进 JSON 会变成乱码。"""
    items = structured_issues(["指标「z」 表达式无法解析: SUM(amount \x1b[4m/\x1b[0m"])
    assert items[0]["code"] == "expr_parse"
    assert "\x1b" not in items[0]["message"]


# ── validate_draft ────────────────────────────────────


def test_validate_clean_upsert_is_ok():
    out = validate_draft(DOC, kind="metric", action="upsert", name="最高成绩",
                         payload={"expression": "MAX(students.grade)"},
                         dialect="sqlite")
    assert out["ok"] is True
    assert out["errors"] == [] and out["warnings"] == []
    assert out["normalized"]["expression"] == "MAX(students.grade)"


def test_validate_reports_undeclared_anchor_with_target():
    """静默补建空壳已改为显式报错 —— validate 必须先于 confirm 看到它。"""
    out = validate_draft(DOC, kind="metric", action="upsert", name="贷款均值",
                         payload={"expression": "AVG(loan.amount)",
                                  "datasets": ["loan"]},
                         dialect="sqlite")
    assert out["ok"] is False
    assert out["errors"][0]["code"] == "dataset_undeclared"
    assert out["errors"][0]["target"] == {"kind": "metric", "name": "贷款均值"}


def test_validate_reports_bad_expression():
    out = validate_draft(DOC, kind="metric", action="upsert", name="坏指标",
                         payload={"expression": "SUM(students.grade /"},
                         dialect="sqlite")
    assert out["ok"] is False
    assert out["errors"][0]["code"] == "expr_parse"
    assert out["normalized"]["expression"] == ""


def test_validate_reports_bad_field_target():
    out = validate_draft(DOC, kind="field", action="upsert", name="grade",
                         payload={"expression": "grade"}, dialect="sqlite")
    assert out["ok"] is False
    assert out["errors"][0]["code"] == "field_target_invalid"


def test_validate_warning_also_blocks():
    """lint 门禁不分级:warning 级问题同样拦 confirm → ok 必须为 False。

    今天的 ``_reject_bad_semantics`` 在 issues 非空即拒绝写入,所以「只有
    warning」的草稿 confirm 一样会 400 —— validate 若报 ok=True 就是在骗人。
    """
    out = validate_draft(DOC, kind="dataset", action="upsert", name="students",
                         payload={"unique_keys": [["ghost_col"]]},
                         dialect="sqlite")
    assert out["errors"] == []
    assert [w["code"] for w in out["warnings"]] == ["unique_keys"]
    assert out["ok"] is False


def test_validate_judges_whole_document():
    """草稿本身干净,但文档已坏 → 同样 ok=False(confirm 会拿整份文档过门禁)。"""
    broken = copy.deepcopy(DOC)
    broken["semantic_model"][0]["metrics"].append(
        dict(broken["semantic_model"][0]["metrics"][0]))
    out = validate_draft(broken, kind="metric", action="upsert", name="新指标",
                         payload={"expression": "COUNT(students.id)"},
                         dialect="sqlite")
    assert out["ok"] is False
    assert [e["code"] for e in out["errors"]] == ["dup_metric"]


def test_validate_is_side_effect_free():
    before = copy.deepcopy(DOC)
    validate_draft(DOC, kind="metric", action="upsert", name="新指标",
                   payload={"expression": "MIN(students.grade)"}, dialect="sqlite")
    validate_draft(DOC, kind="dataset", action="delete", name="courses",
                   dialect="sqlite")
    assert DOC == before


def test_validate_delete_of_missing_entity_matches_confirm():
    """删除不存在的实体 confirm 是静默 no-op → validate 不额外加戏。"""
    out = validate_draft(DOC, kind="metric", action="delete", name="不存在",
                         dialect="sqlite")
    assert out["ok"] is True


def test_validate_normalized_expression_for_field():
    out = validate_draft(DOC, kind="field", action="upsert", name="students.grade",
                         payload={"expression": "grade"}, dialect="sqlite")
    assert out["ok"] is True
    assert out["normalized"]["expression"] == "grade"


def test_validate_delete_has_no_normalized_expression():
    out = validate_draft(DOC, kind="metric", action="delete", name="平均成绩",
                         dialect="sqlite")
    assert out["ok"] is True
    assert out["normalized"]["expression"] == ""


# ── _apply_metric 锚定(方案点名的缺陷) ────────────────


def test_apply_metric_rejects_undeclared_dataset_without_stub():
    """不再静默补建空壳 dataset:显式报错,且文档一个字节都不改。"""
    data = copy.deepcopy(DOC)
    before = copy.deepcopy(data)
    with pytest.raises(ValueError, match="锚定的数据集未声明"):
        _apply_draft(data, {"kind": "metric", "action": "upsert", "name": "均值",
                            "payload": {"expression": "AVG(loan.amount)",
                                        "datasets": ["loan"]}}, "sqlite")
    assert data == before
    assert {d["name"] for d in data["semantic_model"][0]["datasets"]} == {
        "students", "courses"}


def test_apply_metric_accepts_declared_dataset():
    data = copy.deepcopy(DOC)
    _apply_draft(data, {"kind": "metric", "action": "upsert", "name": "课程数",
                        "payload": {"expression": "COUNT(courses.course_id)",
                                    "datasets": ["courses"]}}, "sqlite")
    metrics = {m["name"] for m in data["semantic_model"][0]["metrics"]}
    assert "课程数" in metrics
    # 声明过的数据集不会再多出一条重复条目
    assert [d["name"] for d in data["semantic_model"][0]["datasets"]] == [
        "students", "courses"]


# ── _draft_diff ───────────────────────────────────────


def test_draft_diff_create_has_action_row():
    diff = _draft_diff(DOC, {"kind": "metric", "action": "upsert", "name": "新指标",
                             "payload": {"expression": "MAX(students.grade)"}},
                       "sqlite")
    assert diff["before"] is None
    assert diff["after"]["expression"] == "MAX(students.grade)"
    assert diff["error"] is None
    assert diff["fields"][0] == {"f": "动作", "before": "（不存在）",
                                 "after": "新增指标", "changed": True}
    labels = {row["f"]: row for row in diff["fields"]}
    assert labels["表达式 expression"]["changed"] is True


def test_draft_diff_update_keeps_carryover():
    """只改定义的 upsert:手写 filter / agg_time_dimension 不丢 —— 这正是
    前端重实现 diff 会漂移的地方(carryover 只在服务端)。"""
    diff = _draft_diff(DOC, {"kind": "metric", "action": "upsert", "name": "平均成绩",
                             "payload": {"expression": "AVG(students.grade)",
                                         "definition": "新的口径说明"}},
                       "sqlite")
    rows = {row["f"]: row for row in diff["fields"]}
    assert rows["定义 description"]["changed"] is True
    assert rows["定义 description"]["after"] == "新的口径说明"
    # carryover 保住的键不应显示为「被删除」
    assert diff["after"]["filter"] == "students.grade > 0"
    assert rows["过滤 filter"]["changed"] is False
    assert not any(row["f"] == "动作" for row in diff["fields"])


def test_draft_diff_delete_has_action_row():
    diff = _draft_diff(DOC, {"kind": "dataset", "action": "delete",
                             "name": "courses"}, "sqlite")
    assert diff["after"] is None
    assert diff["fields"][0]["after"] == "删除"
    assert diff["fields"][0]["changed"] is True


def test_draft_diff_error_keeps_no_fake_rows():
    diff = _draft_diff(DOC, {"kind": "metric", "action": "upsert", "name": "坏指标",
                             "payload": {"expression": "SUM(students.grade /"}},
                       "sqlite")
    assert diff["error"] and "表达式无法解析" in diff["error"]
    assert diff["before"] is None and diff["after"] is None
    # 干跑失败 → 不产出任何「新增」行(不能假装知道结果)
    assert diff["fields"] == []


def test_draft_diff_marks_removed_values():
    """upsert 不带 definition → 既有描述消失(description 不在 carryover 里),
    行必须显示 before 有值、after 为空 —— 「悄悄删掉」在卡上要看得见。"""
    diff = _draft_diff(DOC, {"kind": "metric", "action": "upsert", "name": "平均成绩",
                             "payload": {"expression": "AVG(students.grade)"}},
                       "sqlite")
    rows = {row["f"]: row for row in diff["fields"]}
    assert rows["定义 description"]["before"] == "学生平均分"
    assert rows["定义 description"]["after"] == ""
    assert rows["定义 description"]["changed"] is True
