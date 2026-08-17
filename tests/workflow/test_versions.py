"""SQL 版本链 + 回归硬检查测试（Sequential Scaling 缺口 1+2+4）。"""

from trove.workflow.versions import (
    regression_report,
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
