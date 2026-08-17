"""Deterministic validation rule tests (no LLM involved)."""

from trove.workflow.rules import (
    is_count_question,
    is_list_question,
    is_ordered_question,
    is_percent_question,
    validate,
    verify,
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

    def test_percent_integer_division_fails(self):
        """percentage 题的除法未显式 CAST DOUBLE → 整数除法截断,必须拦截。"""
        reason = validate(
            "what percentage of clients",
            "SELECT (COUNT(CASE WHEN gender='M' THEN 1 END) / COUNT(*)) * 100",
            ["pct"], [[44.2623]], 1,
        )
        assert reason and "DOUBLE" in reason

    def test_percent_with_double_cast_passes(self):
        assert validate(
            "what percentage of clients",
            "SELECT CAST(SUM(gender='M') AS DOUBLE) * 100 / COUNT(*)",
            ["pct"], [[44.26229508196721]], 1,
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


# ── verify_step 断言层 ─────────────────────────────────────────


class TestVerifyAPI:
    def test_verify_pass_returns_empty_hits(self):
        reason, hits = verify(
            "average loan amount", "SELECT AVG(amount) FROM loan",
            ["avg"], [[123.4]], 1,
        )
        assert reason is None and hits == []

    def test_verify_fail_returns_named_hit(self):
        """失败时 reason 带规则名前缀, hits 返回结构化断言记录。"""
        reason, hits = verify(
            "List all the withdrawals in cash transactions that the client with the id 3356 makes.",
            "SELECT trans_id, account_id, date, type, operation, amount, balance FROM trans",
            ["trans_id", "account_id", "date", "type", "operation", "amount", "balance"],
            [["1", "2", "3", "4", "5", "6", "7"]], 1, lang="en",
        )
        assert reason and "[F1-b]" in reason
        assert hits and hits[0]["name"] == "F1-b"

    def test_validate_wrapper_still_returns_string(self):
        reason = validate(
            "how many accounts", "SELECT name FROM account",
            ["name"], [["a"], ["b"]], 2, lang="en",
        )
        assert isinstance(reason, str)


class TestF1Shape:
    """F1 形状断言:单值题多行 / 列表题超额列 / 单值题 NULL。"""

    def test_percent_with_superlative_qualifier_multi_row_fails(self):
        """「with biggest number of inhabitants」是单一实体的限定语——答案应是单值。"""
        q = ("For the branch which located in the south Bohemia with biggest "
             "number of inhabitants, what is the percentage of the male clients?")
        reason, hits = verify(
            q,
            "SELECT CAST(SUM(gender='M') AS DOUBLE) * 100 / COUNT(*) FROM client "
            "GROUP BY district_id",
            ["district"], [[0.1]] * 55, 55, lang="en",
        )
        assert reason and "F1-a" in reason

    def test_percent_grouped_multi_row_passes(self):
        """「per district」是分组百分比,多行合法(gender 条件齐全)。"""
        q = "what is the percentage of female clients per district"
        assert verify(
            q,
            "SELECT A2, CAST(SUM(gender = 'F') AS DOUBLE) * 100 / COUNT(*) "
            "FROM client GROUP BY A2",
            ["district"], [["a", 1], ["b", 2]], 2, lang="en",
        ) == (None, [])

    def test_percent_with_average_salary_qualifier_passes(self):
        """「with an average salary of over 10000」非极值限定语——不触发。"""
        q = ("What percentage of clients who opened their accounts in the "
             "district with an average salary of over 10000 are women?")
        assert verify(
            q, "SELECT CAST(SUM(gender = 'F') AS DOUBLE) * 100 / COUNT(*) FROM client",
            ["pct"], [[45.5]], 1, lang="en",
        ) == (None, [])

    def test_list_question_many_columns_fails(self):
        """「List all the withdrawals」只要实体标识,9 列全字段导出是错误形态。"""
        q = "List all the withdrawals in cash transactions that the client with the id 3356 makes."
        reason, hits = verify(
            q,
            "SELECT trans.trans_id, trans.account_id, trans.date, trans.type, "
            "trans.operation, trans.amount, trans.balance, trans.k_symbol, trans.bank "
            "FROM trans",
            ["trans_id", "account_id", "date", "type", "operation",
             "amount", "balance", "k_symbol", "bank"],
            [["1"] * 9], 1, lang="en",
        )
        assert reason and "F1-b" in reason

    def test_list_unemployment_rate_four_columns_fails(self):
        """「list the district and the rate」双实体最多 2 列,4 列说明带了中间值列。"""
        q = ("For loans contracts which are still running where client are in debt, "
             "list the district of the and the state the percentage unemployment "
             "rate increment from year 1995 to 1996.")
        reason, hits = verify(
            q,
            "SELECT district.A2, (CAST(district.A13 AS DOUBLE) - district.A12) "
            "/ district.A12 * 100, district.A12, district.A13 FROM loan "
            "JOIN district",
            ["district", "rate", "unemployment_rate_1995", "unemployment_rate_1996"],
            [["a", 1.5, 2.5, 3.5]], 1, lang="en",
        )
        assert reason and "F1-b" in reason

    def test_list_two_columns_passes(self):
        q = ("List the top nine districts, by descending order, from the highest "
             "to the lowest, the number of female account holders.")
        assert verify(
            q,
            "SELECT d.A2, SUM(c.gender = 'F') FROM account a JOIN client c "
            "ON a.client_id = c.client_id JOIN district d "
            "ON a.district_id = d.district_id "
            "GROUP BY d.A2 ORDER BY SUM(c.gender = 'F') DESC LIMIT 9",
            ["district", "count"], [["a", 10], ["b", 8]], 2, lang="en",
        ) == (None, [])

    def test_list_with_theirdates_guard_passes(self):
        """「with their dates and amounts」显式要求多列,不拦。"""
        q = "List the withdrawals with their dates and amounts"
        assert verify(
            q, "SELECT date, amount FROM trans",
            ["date", "amount"], [["1998-01-01", 100]], 1, lang="en",
        ) == (None, [])

    def test_grouped_list_many_columns_passes(self):
        q = "List the loans per district with their amounts"
        assert verify(
            q, "SELECT district, amount FROM loan",
            ["district", "amount"], [["a", 1], ["b", 2]], 2, lang="en",
        ) == (None, [])

    def test_rate_question_null_fails(self):
        q = ("What was the growth rate of the total amount of loans across all "
             "accounts for a male client between 1996 and 1997?")
        reason, hits = verify(
            q,
            "SELECT CAST((SUM(CASE WHEN YEAR(l.date) = 1997 THEN l.amount ELSE 0 END) "
            "- SUM(CASE WHEN YEAR(l.date) = 1996 THEN l.amount ELSE 0 END)) AS DOUBLE) "
            "/ SUM(CASE WHEN YEAR(l.date) = 1996 THEN l.amount ELSE 0 END) * 100 "
            "FROM loan l JOIN client c ON c.client_id = l.account_id "
            "WHERE c.gender = 'M'",
            ["growth_rate"], [[None]], 1, lang="en",
        )
        assert reason and "F1-d" in reason


class TestF4Ordering:
    """F4 排序语义断言。"""

    def test_descending_question_with_asc_order_fails(self):
        q = ("List the top nine districts, by descending order, from the highest "
             "to the lowest, the number of female account holders.")
        reason, hits = verify(
            q, "SELECT A2, COUNT(*) FROM account GROUP BY A2 ORDER BY COUNT(*) ASC",
            ["district", "count"], [["a", 1], ["b", 2]], 2, lang="en",
        )
        assert reason and "F4-a" in reason

    def test_descending_with_desc_order_passes(self):
        q = ("List the top nine districts, by descending order, from the highest "
             "to the lowest, the number of female account holders.")
        assert verify(
            q,
            "SELECT d.A2, SUM(c.gender = 'F') FROM account a JOIN client c "
            "ON a.client_id = c.client_id JOIN district d "
            "ON a.district_id = d.district_id "
            "GROUP BY d.A2 ORDER BY SUM(c.gender = 'F') DESC",
            ["district", "count"], [["a", 10], ["b", 8]], 2, lang="en",
        ) == (None, [])

    def test_oldest_question_asc_order_passes(self):
        """「oldest」是选择标准不是排序方向词,ORDER BY birth_date ASC 是正确写法。"""
        q = "Name the account numbers of female clients who are oldest and have lowest average salary?"
        assert verify(
            q,
            "SELECT a.account_id FROM client c JOIN account a ON c.client_id = a.client_id "
            "WHERE c.gender = 'F' ORDER BY c.birth_date ASC",
            ["account_id"], [["1"], ["2"]], 2, lang="en",
        ) == (None, [])

    def test_top_n_limit_mismatch_fails(self):
        q = "list the top ten withdrawals"
        reason, hits = verify(
            q, "SELECT * FROM trans ORDER BY amount DESC LIMIT 5",
            ["trans_id"], [["1"]] * 5, 5, lang="en",
        )
        assert reason and "F4-b" in reason

    def test_top_n_limit_match_passes(self):
        q = "list the top ten withdrawals"
        assert verify(
            q, "SELECT * FROM trans ORDER BY amount DESC LIMIT 10",
            ["trans_id"], [[str(i)] for i in range(10)], 10, lang="en",
        ) == (None, [])

    def test_top_without_number_passes(self):
        q = "list the top withdrawals"
        assert verify(
            q, "SELECT * FROM trans ORDER BY amount DESC LIMIT 5",
            ["trans_id"], [[str(i)] for i in range(5)], 5, lang="en",
        ) == (None, [])


class TestF2FilterCoverage:
    """F2 过滤条件覆盖:question 关键词必须在 SQL 中有对应条件。"""

    def test_male_question_without_gender_condition_fails(self):
        q = ("What was the growth rate of the total amount of loans across all "
             "accounts for a male client between 1996 and 1997?")
        reason, hits = verify(
            q,
            "SELECT CAST((SUM(CASE WHEN YEAR(l.date) = 1997 THEN l.amount ELSE 0 END) "
            "- SUM(CASE WHEN YEAR(l.date) = 1996 THEN l.amount ELSE 0 END)) AS DOUBLE) "
            "/ SUM(CASE WHEN YEAR(l.date) = 1996 THEN l.amount ELSE 0 END) * 100 "
            "FROM loan l",
            ["growth_rate"], [[44.26]], 1, lang="en",
        )
        assert reason and "F2-a" in reason

    def test_male_question_with_gender_condition_passes(self):
        q = ("What was the growth rate of the total amount of loans across all "
             "accounts for a male client between 1996 and 1997?")
        sql = (
            "SELECT CAST((SUM(CASE WHEN YEAR(l.date) = 1997 THEN l.amount ELSE 0 END) "
            "- SUM(CASE WHEN YEAR(l.date) = 1996 THEN l.amount ELSE 0 END)) AS DOUBLE) "
            "/ SUM(CASE WHEN YEAR(l.date) = 1996 THEN l.amount ELSE 0 END) * 100 "
            "FROM loan l JOIN client c ON c.client_id = l.account_id "
            "WHERE c.gender = 'M'"
        )
        assert verify(q, sql, ["growth_rate"], [[44.26]], 1, lang="en") == (None, [])

    def test_female_salary_subquery_passes(self):
        """「female average salary」通过子查询里的 gender 条件表达,不误报。"""
        q = "List out the no. of districts that have female average salary less than 2000"
        sql = ("SELECT COUNT(DISTINCT district_id) FROM district d WHERE "
               "(SELECT AVG(A4) FROM account WHERE gender = 'F' "
               "AND district_id = d.district_id) < 2000")
        assert verify(q, sql, ["count"], [[3]], 1, lang="en") == (None, [])

    def test_year_question_without_date_condition_fails(self):
        q = ("Among the accounts who have approved loan date on 1/1/1997, "
             "list out the accounts")
        reason, hits = verify(
            q, "SELECT account_id FROM loan",
            ["account_id"], [["1"], ["2"]], 2, lang="en",
        )
        assert reason and "F2-b" in reason

    def test_year_question_with_year_function_passes(self):
        q = ("Among the accounts who have approved loan date on 1/1/1997, "
             "list out the accounts")
        assert verify(
            q, "SELECT account_id FROM loan WHERE YEAR(loan.date) = 1997",
            ["account_id"], [["1"], ["2"]], 2, lang="en",
        ) == (None, [])

    def test_date_range_question_with_between_passes(self):
        q = "Between 1/1/1995 and 12/31/1997, how many loans in the amount of at least 5000?"
        assert verify(
            q, "SELECT COUNT(*) FROM loan WHERE date BETWEEN '1995-01-01' AND '1997-12-31' AND amount >= 5000",
            ["count"], [[3]], 1, lang="en",
        ) == (None, [])

    def test_year_range_question_skipped(self):
        """「from year 1995 to 1996」可用按年分列(A12/A13)回答,不强制日期条件。"""
        q = ("For loans contracts which are still running where client are in debt, "
             "list the district of the and the state the percentage unemployment "
             "rate increment from year 1995 to 1996.")
        assert verify(
            q, "SELECT A2 FROM district",
            ["district"], [["1.5"]], 1, lang="en",
        ) == (None, [])

    def test_account_number_three_no_year_passes(self):
        q = "How often does account number 3 request an account statement to be released?"
        assert verify(
            q, "SELECT frequency FROM account WHERE account_id = 3",
            ["frequency"], [["POPLATEK MESICNE"]], 1, lang="en",
        ) == (None, [])

    def test_region_question_without_condition_fails(self):
        q = "how many accounts are staying in East Bohemia region?"
        reason, hits = verify(
            q, "SELECT COUNT(*) FROM account WHERE frequency = 'POPLATEK PO OBRATU'",
            ["count"], [[100]], 1, lang="en",
        )
        assert reason and "F2-c" in reason

    def test_region_with_a3_condition_passes(self):
        q = "how many accounts are staying in East Bohemia region?"
        sql = ("SELECT COUNT(*) FROM account a JOIN district d "
               "ON a.district_id = d.district_id WHERE d.A3 = 'east Bohemia'")
        assert verify(q, sql, ["count"], [[100]], 1, lang="en") == (None, [])

    def test_credit_card_question_without_condition_fails(self):
        q = ("Who are the account holder identification numbers whose who have "
             "transactions on the credit card with the amount is less than the "
             "average, in 1998?")
        reason, hits = verify(
            q, "SELECT DISTINCT account_id FROM trans WHERE date BETWEEN '1998-01-01' AND '1998-12-31'",
            ["account_id"], [["1"], ["2"]], 2, lang="en",
        )
        assert reason and "F2-d" in reason

    def test_credit_card_with_kartou_passes(self):
        q = ("Who are the account holder identification numbers whose who have "
             "transactions on the credit card with the amount is less than the "
             "average, in 1998?")
        sql = ("SELECT DISTINCT account_id FROM trans WHERE operation = 'VYBER KARTOU' "
               "AND date BETWEEN '1998-01-01' AND '1998-12-31'")
        assert verify(q, sql, ["account_id"], [["1"], ["2"]], 2, lang="en") == (None, [])

    def test_credit_card_with_card_table_passes(self):
        q = "Provide the IDs and age of the client with high level credit card, which is eligible for loans."
        sql = ("SELECT c.client_id, YEAR(CURRENT_DATE) - YEAR(c.birth_date) "
               "FROM card cd JOIN disp d ON cd.disp_id = d.disp_id "
               "JOIN client c ON d.client_id = c.client_id")
        assert verify(q, sql, ["client_id", "age"], [["1", 30]], 1, lang="en") == (None, [])


class TestF3ValueRules:
    """F3 值域/类型/唯一性断言。"""

    def test_average_question_non_numeric_fails(self):
        q = "what is the average loan amount"
        reason, hits = verify(
            q, "SELECT name FROM loan LIMIT 1",
            ["name"], [["abc"]], 1, lang="en",
        )
        assert reason and "F3-a" in reason

    def test_average_numeric_passes(self):
        q = "what is the average loan amount"
        assert verify(q, "SELECT AVG(amount) FROM loan", ["avg"], [[1234.5]], 1, lang="en") == (None, [])

    def test_date_question_non_numeric_passes(self):
        """「what is the date」不期望数值,不触发 dtype 断言。"""
        q = "what is the date of the first loan"
        assert verify(
            q, "SELECT MIN(date) FROM loan", ["date"], [["1998-01-01"]], 1, lang="en",
        ) == (None, [])

    def test_list_duplicate_ids_fails(self):
        q = "List the accounts that have running contracts"
        reason, hits = verify(
            q, "SELECT account_id, amount FROM loan",
            ["account_id", "amount"], [["1", 10], ["1", 20], ["2", 30]], 3, lang="en",
        )
        assert reason and "F3-b" in reason

    def test_list_unique_ids_passes(self):
        q = "List the accounts that have running contracts"
        assert verify(
            q, "SELECT account_id, amount FROM loan",
            ["account_id", "amount"], [["1", 10], ["2", 20]], 2, lang="en",
        ) == (None, [])

    def test_list_district_names_dups_passes(self):
        """第一列是实体名(district)非 ID 列,重复合法(多个客户同区)。"""
        q = "List the districts of the clients"
        assert verify(
            q, "SELECT district FROM client",
            ["district"], [["a"], ["a"], ["b"]], 3, lang="en",
        ) == (None, [])

    def test_grouped_list_dups_passes(self):
        q = "List the loans per district"
        assert verify(
            q, "SELECT district, loan_id FROM loan",
            ["district", "loan_id"], [["a", 1], ["a", 2]], 2, lang="en",
        ) == (None, [])

    def test_growth_rate_absurd_value_fails(self):
        q = ("What was the growth rate of the total amount of loans across all "
             "accounts for a male client between 1996 and 1997?")
        reason, hits = verify(
            q,
            "SELECT CAST((SUM(CASE WHEN YEAR(l.date) = 1997 THEN l.amount ELSE 0 END) "
            "- SUM(CASE WHEN YEAR(l.date) = 1996 THEN l.amount ELSE 0 END)) AS DOUBLE) "
            "/ SUM(CASE WHEN YEAR(l.date) = 1996 THEN l.amount ELSE 0 END) * 100 "
            "FROM loan l JOIN client c ON c.client_id = l.account_id "
            "WHERE c.gender = 'M'",
            ["growth_rate"], [[500000.0]], 1, lang="en",
        )
        assert reason and "F3-c" in reason

    def test_growth_rate_normal_value_passes(self):
        q = ("What was the growth rate of the total amount of loans across all "
             "accounts for a male client between 1996 and 1997?")
        sql = (
            "SELECT CAST((SUM(CASE WHEN YEAR(l.date) = 1997 THEN l.amount ELSE 0 END) "
            "- SUM(CASE WHEN YEAR(l.date) = 1996 THEN l.amount ELSE 0 END)) AS DOUBLE) "
            "/ SUM(CASE WHEN YEAR(l.date) = 1996 THEN l.amount ELSE 0 END) * 100 "
            "FROM loan l JOIN client c ON c.client_id = l.account_id "
            "WHERE c.gender = 'M'"
        )
        assert verify(q, sql, ["growth_rate"], [[44.26]], 1, lang="en") == (None, [])


class TestGuardRegressions:
    """预检误伤回归:这些 MATCH 形态不得被断言层拦截。"""

    def test_female_average_salary_metric_phrase_passes(self):
        """「female average salary is more than X」是度量描述,gender 过滤可能冗余。"""
        q = ("List out the no. of districts that have female average salary is "
             "more than 6000 but less than 10000?")
        assert verify(
            q,
            "SELECT COUNT(*) FROM district WHERE A11 > 6000 AND A11 < 10000",
            ["count"], [[5]], 1, lang="en",
        ) == (None, [])

    def test_per_year_column_question_passes(self):
        """「crimes committed in 1995」可由按年分列(A15)回答,不强制日期条件。"""
        q = ("In the branch where the second-highest number of crimes were "
             "committed in 1995 occurred, how many male clients are there?")
        sql = ("SELECT COUNT(client.client_id) FROM client JOIN district "
               "ON client.district_id = district.district_id "
               "WHERE district.district_id = "
               "(SELECT district_id FROM district ORDER BY A15 DESC LIMIT 1 OFFSET 1) "
               "AND client.gender = 'M'")
        assert verify(q, sql, ["count"], [[100]], 1, lang="en") == (None, [])

    def test_three_entity_enumeration_passes(self):
        """「list the account ID, district name and district region」显式枚举 3 实体,3 列合法。"""
        q = ("For accounts in 1993 with statement issued after transaction, "
             "list the account ID, district name and district region.")
        sql = ("SELECT account.account_id, district.A2, district.A3 "
               "FROM account JOIN district ON account.district_id = district.district_id "
               "WHERE YEAR(account.date) = 1993 "
               "AND account.frequency = 'POPLATEK PO OBRATU'")
        assert verify(
            q, sql,
            ["account_id", "district", "region"],
            [["1", "Hl.m. Praha", "Prague"]], 1, lang="en",
        ) == (None, [])

    def test_cash_withdrawals_via_vyber_passes(self):
        """BIRD 数据集里 cash 用 operation='VYBER' 表达,词面无 pokl/cash 不拦。"""
        q = ("List all the withdrawals in cash transactions that the client "
             "with the id 3356 makes.")
        sql = ("SELECT T4.trans_id FROM client T1 JOIN disp T2 "
               "ON T1.client_id = T2.client_id JOIN account T3 "
               "ON T2.account_id = T3.account_id JOIN trans T4 "
               "ON T3.account_id = T4.account_id "
               "WHERE T1.client_id = 3356 AND T4.operation = 'VYBER'")
        assert verify(q, sql, ["trans_id"], [["1"]], 1, lang="en") == (None, [])


class TestF2DateBucket:
    """F2-b 只检查具体日期(1/1/1995),裸年份(1997)不再触发。"""

    def test_date_question_without_condition_fails(self):
        q = "Between 1/1/1995 and 12/31/1997, how many loans in the amount of at least 5000?"
        reason, hits = verify(
            q, "SELECT COUNT(*) FROM loan WHERE amount >= 5000",
            ["count"], [[3]], 1, lang="en",
        )
        assert reason and "F2-b" in reason

    def test_bare_year_without_condition_passes(self):
        """「approved loan date in 1997」裸年份不再强制日期条件(避免按年分列误伤)。"""
        q = "Among the accounts who have approved loan date in 1997, list out the accounts"
        assert verify(
            q, "SELECT account_id FROM loan",
            ["account_id"], [["1"], ["2"]], 2, lang="en",
        ) == (None, [])
