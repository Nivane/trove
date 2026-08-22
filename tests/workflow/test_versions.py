"""SQL 版本链 + 回归硬检查测试（Sequential Scaling 缺口 1+2+4）。"""

from trove.workflow.versions import (
    extract_rule_hits,
    is_validator_conflict,
    record_version,
    regression_report,
    regression_state,
    result_sig,
)


class TestResultSig:
    def test_order_and_type_insensitive(self):
        assert result_sig([[1, "a"], [2, "b"]]) == result_sig([["2", "b"], ["1", "a"]])

    def test_different_rows_differ(self):
        assert result_sig([[1]]) != result_sig([[2]])

    def test_empty_rows_signature(self):
        assert result_sig([]) == result_sig([])


class TestRegressionReport:
    """修正轮 vs 上一版: 无效修复 / 无进展 / 问题转移 三态判定。"""

    def test_identical_signature_reports_invalid_fix(self):
        """结果签名与上一版相同 → 修复无效（很可能复制了旧错误）。"""
        prev = {"round": 1, "sig": "same", "issues": []}
        report = regression_report(prev, "same", [])
        assert report is not None
        assert "invalid" in report.lower()
        assert "Round 1" in report

    def test_overlapping_rules_report_no_progress(self):
        """结果变了但同一规则族再挂 → 无进展。"""
        prev = {"round": 1, "sig": "old", "issues": ["F1-a"]}
        report = regression_report(prev, "new-sig", ["F1-a"])
        assert report is not None
        assert "F1-a" in report
        assert "no progress" in report.lower()

    def test_new_rule_reports_problem_shift(self):
        """旧规则修好了但引入新规则 → 问题转移（修 A 引入 B）。"""
        prev = {"round": 1, "sig": "old", "issues": ["F1-a"]}
        report = regression_report(prev, "new-sig", ["F1-b"])
        assert report is not None
        assert "F1-b" in report
        assert "shift" in report.lower() or "new" in report.lower()

    def test_progress_without_report(self):
        """结果变了且无规则命中 → 有进展（或本就不是规则失败）→ 无报告。"""
        prev = {"round": 1, "sig": "old", "issues": ["F1-a"]}
        assert regression_report(prev, "new-sig", []) is None

    def test_no_previous_version_no_report(self):
        assert regression_report(None, "sig", []) is None

    def test_consensus_failures_compare_by_signature_only(self):
        """无规则命中的失败（投票平局/执行错误）只做签名对比。"""
        prev = {"round": 2, "sig": "same", "issues": []}
        report = regression_report(prev, "same", [])
        assert "invalid" in report.lower()

    def test_partial_overlap_reports_no_progress_on_common_rule(self):
        """新旧规则有交集 → 无进展报告指出公共规则。"""
        prev = {"round": 1, "sig": "old", "issues": ["F1-a", "F2-a"]}
        report = regression_report(prev, "new-sig", ["F1-a", "F1-b"])
        assert report is not None
        assert "F1-a" in report
        assert "F1-b" in report  # 新增规则也提示（问题转移部分）


class TestRegressionState:
    """regression_state: 修复进展量化标签（置信度信号 + no_progress 计数的数据源）。"""

    def test_no_previous_version_is_first(self):
        assert regression_state(None, "sig", []) == "first"

    def test_identical_signature_is_invalid(self):
        prev = {"round": 1, "sig": "same", "issues": []}
        assert regression_state(prev, "same", []) == "invalid"

    def test_overlapping_rules_is_none(self):
        prev = {"round": 1, "sig": "old", "issues": ["F1-b"]}
        assert regression_state(prev, "new", ["F1-b", "F2-a"]) == "none"

    def test_only_new_rules_is_shift(self):
        prev = {"round": 1, "sig": "old", "issues": ["F1-b"]}
        assert regression_state(prev, "new", ["F2-a"]) == "shift"

    def test_no_overlap_no_new_is_improved(self):
        prev = {"round": 1, "sig": "old", "issues": ["F1-b"]}
        assert regression_state(prev, "new", []) == "improved"

    def test_execution_error_signature_change_is_improved(self):
        """无规则命中的失败(执行错误/投票)签名变化 → 在动,视为有进展。"""
        prev = {"round": 1, "sig": "old", "issues": []}
        assert regression_state(prev, "new", []) == "improved"


class TestValidatorConflictGuard:
    """校验冲突护栏:answer/extra-columns 类命中 + 同签名 ≠ 复制旧错误。

    回归:正确 SQL(聚合别名被 extra-columns 误判)每轮同签名,曾被判
    invalid → 无进展轮累加 → 优雅降级。现在应判 validator-conflict。"""

    def test_extract_rule_hits_captures_validator_conflicts(self):
        assert extract_rule_hits("Plan check [extra-columns]: ...") == ["extra-columns"]
        assert extract_rule_hits("[answer-columns] foo") == ["answer-columns"]
        assert extract_rule_hits("[F1-b] list too wide") == ["F1-b"]
        # 混合:保序去重
        assert extract_rule_hits(
            "[F1-a] x [extra-columns] y [extra-columns]",
        ) == ["F1-a", "extra-columns"]

    def test_is_validator_conflict(self):
        assert is_validator_conflict(["extra-columns"])
        assert is_validator_conflict(["answer-columns"])
        assert not is_validator_conflict([])
        assert not is_validator_conflict(["F1-a"])
        assert not is_validator_conflict(["F1-a", "extra-columns"])

    def test_record_version_marks_validator_conflict(self):
        v = record_version([], "SELECT 1", "sig", ["extra-columns"], 1, "err")
        assert v[0]["validator_conflict"] is True
        v2 = record_version([], "SELECT 1", "sig", ["F1-a"], 1, "err")
        assert v2[0]["validator_conflict"] is False

    def test_same_signature_validator_conflict_not_invalid(self):
        """上一版校验冲突 + 当前同签名且仍同类命中 → validator-conflict。"""
        prev = {"round": 1, "sig": "same", "issues": ["extra-columns"],
                "validator_conflict": True}
        assert regression_state(prev, "same", ["extra-columns"]) == "validator-conflict"

    def test_validator_conflict_report_guides_replan(self):
        prev = {"round": 1, "sig": "same", "issues": ["extra-columns"],
                "validator_conflict": True}
        report = regression_report(prev, "same", ["extra-columns"])
        assert report is not None
        assert "validator conflict" in report.lower()
        assert "re-plan" in report.lower()

    def test_same_signature_validator_conflict_wrong_rule_still_invalid(self):
        """上一版校验冲突但当前是新规则类命中 → 仍判 invalid(不是误报复现)。"""
        prev = {"round": 1, "sig": "same", "issues": ["extra-columns"],
                "validator_conflict": True}
        assert regression_state(prev, "same", ["F1-b"]) == "invalid"
