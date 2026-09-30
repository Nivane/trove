"""validator 档的执行器 —— 聚合级断言,零 LLM,三值判定。"""

from __future__ import annotations

from trove.services.decision.expr import UNKNOWN
from trove.services.skills.validators import build_scope, run_validators


def _spec(expr, columns=None, message="violated", severity="advisory", mode=None):
    spec = {
        "name": "g1", "severity": severity,
        "checks": [{"expr": expr, "message": message}],
    }
    if columns is not None:
        spec["checks"][0]["columns"] = columns
    if mode is not None:
        spec["mode"] = mode
    return spec


# ── build_scope ──────────────────────────────────────────────

def test_scope_pools_named_columns():
    """点名多列时聚合是**合并**的:任一列为负 → min < 0。"""
    scope = build_scope(
        {"columns": ["a", "b"]}, ["a", "b"], [[1, -2], [3, 4]], row_count=2,
    )
    assert scope["min"] == -2.0
    assert scope["max"] == 4.0
    assert scope["sum"] == 6.0
    assert scope["avg"] == 1.5


def test_scope_missing_column_is_unknown():
    """点名的列一个都不在结果里 → 判不了,不是通过。"""
    scope = build_scope({"columns": ["nope"]}, ["a"], [[1]], row_count=1)
    assert scope["min"] is UNKNOWN
    assert scope["max"] is UNKNOWN


def test_scope_non_numeric_cells_are_skipped_not_zeroed():
    """文本/None 单元格跳过,不折算成 0.0 —— 0.0 是自信的'没越界'。"""
    scope = build_scope({"columns": ["a"]}, ["a"], [["N/A"], [5]], row_count=2)
    assert scope["min"] == 5.0
    assert scope["sum"] == 5.0


def test_scope_counts_nulls():
    scope = build_scope({"columns": ["a"]}, ["a"], [[None], [1], ["x"]], row_count=3)
    assert scope["null_count"] == 2.0        # None + 非数值


def test_scope_row_count_uses_authoritative_value():
    """行的权威数字是 state.row_count(结果可能被展示层截断)。"""
    scope = build_scope({}, ["a"], [[1]], row_count=999)
    assert scope["row_count"] == 999.0
    assert scope["col_count"] == 1.0


# ── run_validators:三值 ──────────────────────────────────────

def test_verdict_true_when_all_checks_pass():
    hits = run_validators([_spec("min >= 0", ["a"])], columns=["a"], rows=[[1], [2]],
                          row_count=2)
    assert hits[0]["verdict"] is True
    assert hits[0]["name"] == "g1"


def test_verdict_false_on_violation():
    hits = run_validators([_spec("min >= 0", ["a"])], columns=["a"], rows=[[1], [-2]],
                          row_count=2)
    assert hits[0]["verdict"] is False
    assert hits[0]["message"] == "violated"


def test_verdict_none_when_column_absent():
    """缺列必须落 None(判不了),绝不能塌成 True 或 False。"""
    hits = run_validators([_spec("min >= 0", ["nope"])], columns=["a"], rows=[[1]],
                          row_count=1)
    assert hits[0]["verdict"] is None


def test_verdict_none_on_bad_expression_instead_of_raising():
    """表达式写错不得让管线崩 —— 降级为'判不了',不是'通过'。"""
    hits = run_validators([_spec("min >=> 0", ["a"])], columns=["a"], rows=[[1]],
                          row_count=1)
    assert hits[0]["verdict"] is None
    assert "expression" in hits[0]["message"].lower()


def test_first_failing_check_wins_within_one_validator():
    hits = run_validators(
        [{"name": "g1", "severity": "advisory", "checks": [
            {"expr": "min >= 0", "columns": ["a"], "message": "neg"},
            {"expr": "row_count > 100", "message": "too few"},
        ]}],
        columns=["a"], rows=[[-1]], row_count=1,
    )
    assert hits[0]["verdict"] is False
    assert hits[0]["message"] == "neg"


def test_row_count_check_needs_no_columns():
    hits = run_validators([_spec("row_count > 0", [])], columns=["a"], rows=[],
                          row_count=0)
    assert hits[0]["verdict"] is False


def test_llm_mode_reports_cannot_evaluate_not_silence():
    """mode: llm 本期不驱动 —— 但**不许静默消失**。

    直接 `continue` 掉是最省事的写法,也是最坏的:声明了却没人管 = 静默
    失效,管理员看到"配好了、确认了",实际什么都没发生。三值里现成的答案
    是 None(判不了)—— 它不进用户附注(output 只报 advisory 的明确违反),
    但会落进 validator_hits 与质检统计,于是"没人管"是可观测的。
    """
    hits = run_validators([_spec("min >= 0", ["a"], mode="llm")],
                          columns=["a"], rows=[[1]], row_count=1)
    assert len(hits) == 1
    assert hits[0]["verdict"] is None
    assert "llm" in hits[0]["message"].lower()


def test_severity_is_carried_through():
    hits = run_validators([_spec("min >= 0", ["a"], severity="blocking")],
                          columns=["a"], rows=[[-1]], row_count=1)
    assert hits[0]["severity"] == "blocking"


# ── 截断:窗口内的聚合不是整个结果的聚合 ──────────────────────

def test_truncated_rows_make_aggregates_unknown():
    """结果被展示上限截断时,内容聚合只覆盖窗口 —— 判不了,不是通过。

    ``rows`` 是窗口(execute_sql 按 max_rows 截断),``row_count`` 是真实
    总数。窗口内算出的 min/sum/null_count 都不是整个结果的对应值:拿它
    报"通过"正是本模块最反对的"没查却报平安"。
    """
    scope = build_scope({"columns": ["a"]}, ["a"], [[1], [2]], row_count=5000)
    for k in ("min", "max", "sum", "avg", "null_count"):
        assert scope[k] is UNKNOWN, k
    assert scope["row_count"] == 5000.0
    assert scope["col_count"] == 1.0


def test_untruncated_rows_still_compute():
    """守卫的反面:row_count 与窗口等长时照常算 —— 别把守卫写成恒 UNKNOWN。"""
    scope = build_scope({"columns": ["a"]}, ["a"], [[1], [2]], row_count=2)
    assert scope["min"] == 1.0
    assert scope["sum"] == 3.0


def test_truncated_result_verdict_is_none_not_true():
    hits = run_validators([_spec("min >= 0", ["a"])], columns=["a"],
                          rows=[[1]], row_count=9999)
    assert hits[0]["verdict"] is None


# ── 空 checks / 形状写错:降级,不静默通过 ────────────────────

def test_empty_checks_is_none_not_true():
    """声明了却没有任何检查 = 判不了。静默的"通过"和"没人管"一样坏。"""
    hits = run_validators([{"name": "g1", "severity": "advisory", "checks": []}],
                          columns=["a"], rows=[[1]], row_count=1)
    assert hits[0]["verdict"] is None
    assert "no checks" in hits[0]["message"].lower()


def test_missing_checks_key_is_none_not_true():
    hits = run_validators([{"name": "g1", "severity": "advisory"}],
                          columns=["a"], rows=[[1]], row_count=1)
    assert hits[0]["verdict"] is None


def test_non_dict_spec_degrades_instead_of_raising():
    """SKILL.md 是可以手改的 YAML —— 形状写错不得把管线炸掉。"""
    hits = run_validators([None, _spec("min >= 0", ["a"])],
                          columns=["a"], rows=[[1]], row_count=1)
    assert len(hits) == 2
    assert hits[0]["verdict"] is None
    assert hits[1]["verdict"] is True


def test_mapping_checks_degrades_instead_of_raising():
    """``checks`` 写成映射(常见手误)时迭代出的是键(字符串)。"""
    hits = run_validators([{"name": "g1", "severity": "advisory",
                            "checks": {"expr": "min >= 0"}}],
                          columns=["a"], rows=[[1]], row_count=1)
    assert hits[0]["verdict"] is None
    assert "check error" in hits[0]["message"].lower()


def test_scalar_checks_degrades_instead_of_raising():
    """``checks: 5`` / ``checks: yes`` —— 手写 YAML 最省事的笔误,不得炸管线。"""
    hits = run_validators([{"name": "g1", "severity": "advisory", "checks": 5}],
                          columns=["a"], rows=[[1]], row_count=1)
    assert hits[0]["verdict"] is None
    assert "checks" in hits[0]["message"].lower()


def test_string_row_count_still_coerces():
    """row_count 归一化后再比较 —— 可强转的字符串照常算,不因守卫退化。"""
    scope = build_scope({"columns": ["a"]}, ["a"], [[1], [2]], row_count="2")
    assert scope["row_count"] == 2.0
    assert scope["min"] == 1.0
