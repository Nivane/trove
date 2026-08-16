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
            ["name"], [["a"], ["b"]], 2, lang="en",
        )
        assert reason and "single number" in reason

    def test_count_question_single_value_passes(self):
        assert validate(
            "how many accounts", "SELECT COUNT(*) FROM account",
            ["count"], [[123]], 1,
        ) is None

    def test_count_question_null_fails(self):
        reason = validate(
            "how many accounts", "SELECT COUNT(*) FROM account",
            ["count"], [[None]], 1,
        )
        assert reason and "NULL" in reason

    def test_default_lang_is_chinese(self):
        """默认中文:失败原因按 lang 输出中文。"""
        reason = validate(
            "how many accounts", "SELECT COUNT(*) FROM account",
            ["count"], [[None]], 1,
        )
        assert reason and "计数问题返回了 NULL" in reason

    def test_count_question_string_value_fails(self):
        """计数结果必须是数值（dtype 检查），字符串说明聚合/列选错了。"""
        reason = validate(
            "how many accounts", "SELECT name FROM account LIMIT 1",
            ["name"], [["Alice"]], 1, lang="en",
        )
        assert reason and "numeric" in reason

    def test_count_question_negative_fails(self):
        """计数不可能为负（值域检查）。"""
        reason = validate(
            "how many accounts", "SELECT COUNT(*) - 100 FROM account",
            ["count"], [[-5]], 1, lang="en",
        )
        assert reason and "negative" in reason

    def test_count_question_float_passes(self):
        assert validate(
            "how many accounts", "SELECT COUNT(*) FROM account",
            ["count"], [[123.0]], 1,
        ) is None

    def test_list_question_zero_rows_fails(self):
        reason = validate(
            "list all withdrawals", "SELECT * FROM trans WHERE 1=0",
            ["id"], [], 0, lang="en",
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


class TestScopeAmbiguityRule:
    """极值作用域歧义:MIN/MAX 子查询 + 外层还有过滤条件 → 告警回查。"""

    BAD_SQL = """
SELECT a.account_id, a.frequency, l.amount, l.date
FROM loan l
JOIN account a ON l.account_id = a.account_id
WHERE YEAR(l.date) = 1997
  AND l.amount = (SELECT MIN(amount) FROM loan WHERE YEAR(date) = 1997)
  AND a.frequency = 'POPLATEK TYDNE'
"""

    def test_minmax_subquery_with_outer_filters_flags(self):
        q = ("Among the accounts who have approved loan date in 1997, "
             "list out the accounts that have the lowest approved amount "
             "and choose weekly issuance statement.")
        reason = validate(q, self.BAD_SQL, ["account_id"], [[176]], 1, lang="en")
        assert reason and "scope" in reason

    def test_chinese_question_flags_too(self):
        q = "在1997年批准了贷款的账户中，列出批准金额最低、且选择周发放报表的账户。"
        reason = validate(q, self.BAD_SQL, ["account_id"], [[176]], 1)
        assert reason and "范围" in reason

    def test_no_plain_outer_filter_passes(self):
        """外层 WHERE 只有极值子查询本身(无其它条件)→ 不告警。"""
        q = "list the accounts that have the lowest approved amount"
        sql = ("SELECT account_id FROM loan "
               "WHERE amount = (SELECT MIN(amount) FROM loan)")
        assert validate(q, sql, ["account_id"], [[176]], 1, lang="en") is None

    def test_question_without_extreme_word_passes(self):
        """问题不含最低/最高等极值词 → 规则不介入(防误报)。"""
        q = "list the accounts approved in 1997"
        assert validate(q, self.BAD_SQL, ["account_id"], [[176]], 1, lang="en") is None

    def test_plain_min_query_without_subquery_passes(self):
        """MIN 直接在外层聚合(无子查询)是标准写法 → 不告警。"""
        q = "what is the lowest approved amount"
        sql = "SELECT MIN(amount) FROM loan WHERE YEAR(date) = 1997"
        assert validate(q, sql, ["amount"], [[1000]], 1, lang="en") is None

    def test_duplicated_filters_inside_subquery_pass(self):
        """过滤已正确放进 MIN 子查询内(外层为冗余重复)→ 不报警。

        这是模型按正确语义生成的形态:子查询 WHERE 含 year+frequency,
        外层重复同样条件。别名差异(l/a vs l2/a2)必须被识别为等价。"""
        q = ("Among the accounts who have approved loan date in 1997, "
             "list out the accounts that have the lowest approved amount "
             "and choose weekly issuance statement.")
        sql = """
SELECT DISTINCT l.account_id
FROM loan l
JOIN account a ON l.account_id = a.account_id
WHERE YEAR(l.date) = 1997
  AND a.frequency = 'POPLATEK TYDNE'
  AND l.amount = (
      SELECT MIN(l2.amount)
      FROM loan l2
      JOIN account a2 ON l2.account_id = a2.account_id
      WHERE YEAR(l2.date) = 1997
        AND a2.frequency = 'POPLATEK TYDNE'
  )
"""
        assert validate(q, sql, ["account_id"], [[176]], 1, lang="en") is None

    def test_partially_missing_filter_still_flags(self):
        """子查询内只有 year、缺 frequency(外层有)→ 仍报警。"""
        q = "list the accounts that have the lowest approved amount in 1997"
        sql = """
SELECT DISTINCT l.account_id
FROM loan l
JOIN account a ON l.account_id = a.account_id
WHERE YEAR(l.date) = 1997
  AND a.frequency = 'POPLATEK TYDNE'
  AND l.amount = (
      SELECT MIN(l2.amount) FROM loan l2 WHERE YEAR(l2.date) = 1997
  )
"""
        reason = validate(q, sql, ["account_id"], [[176]], 1, lang="en")
        assert reason and "scope" in reason
