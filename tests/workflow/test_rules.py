"""Deterministic validation rule tests (no LLM involved)."""

from trove.workflow.rules import (
    is_count_question,
    is_list_question,
    is_ordered_question,
    is_percent_question,
    validate,
)


class TestClassification:
    def test_count_english(self):
        assert is_count_question("how many accounts have running contracts")
        assert is_count_question("what is the total amount of loans")
        assert is_count_question("the number of male clients")

    def test_count_chinese(self):
        assert is_count_question("有多少个客户")
        assert is_count_question("贷款总数是多少")

    def test_count_excludes_grouped_questions(self):
        """「每/按…分」的分组统计不算单值 count。"""
        assert not is_count_question("how many accounts per district")
        assert not is_count_question("每个地区的贷款数量")
        assert not is_count_question("学生们的平均成绩是多少")  # 是多少 ≠ 多少个

    def test_list_precedence_over_count(self):
        """「List the no. of X」是 list 问题（gold 返回多行），count 不抢跑。"""
        q = "List out the no. of districts that have female average salary less than 2000"
        assert is_list_question(q)
        assert not is_count_question(q)

    def test_which_grouping_is_not_count(self):
        """「哪个地区…总数」是分组问题，不是单值 count。"""
        assert not is_count_question("哪个地区的贷款金额总数和平均贷款金额最大？")
        assert not is_count_question("which district has the largest total loan amount")

    def test_multi_metric_conjunction_is_not_count(self):
        """「总数和平均」这类多指标问题不是单值 count。"""
        assert not is_count_question("贷款金额总数和平均金额分别是多少")

    def test_each_grouping_is_not_count(self):
        assert not is_count_question("how many accounts in each branch")

    def test_list_english_and_chinese(self):
        assert is_list_question("list the top ten withdrawals")
        assert is_list_question("which districts have most loans")
        assert is_list_question("列出所有客户")

    def test_percent(self):
        assert is_percent_question("what percentage of clients")
        assert is_percent_question("male clients 占比")

    def test_ordered(self):
        assert is_ordered_question("list the top ten withdrawals")
        assert is_ordered_question("排名前十的地区")
        assert not is_ordered_question("which districts have most loans")

    def test_plain_questions_match_nothing(self):
        assert not is_count_question("哪个地区的平均贷款金额最高?")
        assert not is_list_question("哪个地区的平均贷款金额最高?")
        assert not is_percent_question("哪个地区的平均贷款金额最高?")


class TestValidate:
    def test_count_question_multi_rows_fails(self):
        reason = validate(
            "how many accounts", "SELECT name FROM account",
            ["name"], [["a"], ["b"]], 2,
        )
        assert reason and "single number" in reason

    def test_count_question_single_value_passes(self):
        assert validate(
            "how many accounts", "SELECT COUNT(*) FROM account",
            ["count"], [[123]], 1,
        ) is None

    def test_list_question_zero_rows_fails(self):
        reason = validate(
            "list all withdrawals", "SELECT * FROM trans WHERE 1=0",
            ["id"], [], 0,
        )
        assert reason and "no rows" in reason

    def test_plain_question_zero_rows_passes(self):
        assert validate(
            "average grade", "SELECT AVG(grade) FROM students",
            ["avg"], [], 0,
        ) is None

    def test_percent_out_of_range_fails(self):
        reason = validate(
            "what percentage of clients", "SELECT 150.0",
            ["pct"], [[150.0]], 1,
        )
        assert reason and "0-100" in reason

    def test_percent_in_range_passes(self):
        assert validate(
            "what percentage of clients", "SELECT 45.5",
            ["pct"], [[45.5]], 1,
        ) is None

    def test_percent_none_value_fails(self):
        reason = validate(
            "what percentage of clients", "SELECT NULL",
            ["pct"], [[None]], 1,
        )
        assert reason

    def test_top_n_with_limit_but_no_order_by_fails(self):
        reason = validate(
            "list the top ten withdrawals", "SELECT * FROM trans LIMIT 10",
            ["id"], [["x"]], 10,
        )
        assert reason and "ORDER BY" in reason

    def test_top_n_with_order_by_passes(self):
        assert validate(
            "list the top ten withdrawals",
            "SELECT * FROM trans ORDER BY amount DESC LIMIT 10",
            ["id"], [["x"]], 10,
        ) is None

    def test_no_matching_rule_passes(self):
        assert validate(
            "average loan amount", "SELECT AVG(amount) FROM loan",
            ["avg"], [[123.4]], 1,
        ) is None
