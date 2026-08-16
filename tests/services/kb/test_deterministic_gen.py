"""Deterministic KB terms/templates generation tests.

kb init 的 semantics/examples 部分从「LLM 起草」改为「确定性生成」:
有列描述才能起名(A5~A16 这类不透明列不生成,避免 LLM 瞎猜映射)。
"""

from trove.services.kb.deterministic_gen import generate_terms, generate_templates

TABLES = [
    {
        "name": "account",
        "description": "银行账户信息表，包含账户ID、所属地区、对账单发放频率和开户日期",
        "columns": [
            {"name": "account_id", "type": "int", "description": "账户唯一标识符", "enums": []},
            {"name": "district_id", "type": "int", "description": "账户所属地区ID，关联district表", "enums": []},
            {"name": "frequency", "type": "varchar", "description": "账户对账单发放频率（issuance frequency）", "enums": []},
            {"name": "date", "type": "date", "description": "账户开户日期", "enums": []},
        ],
        "metrics": [],
    },
    {
        "name": "loan",
        "description": "贷款信息表，包含贷款ID、账户ID、日期、金额、期限和状态",
        "columns": [
            {"name": "loan_id", "type": "int", "description": "贷款唯一标识符", "enums": []},
            {"name": "amount", "type": "int", "description": "贷款金额", "enums": []},
            {"name": "duration", "type": "int", "description": "贷款期限（月数）", "enums": []},
            {"name": "status", "type": "varchar", "description": "贷款状态", "enums": []},
        ],
        "metrics": [],
    },
]


class TestGenerateTerms:
    def test_count_term_per_table_with_aliases(self):
        terms = generate_terms(TABLES)
        account = {t["term"]: t for t in terms if "account" in t["tables"]}
        count = next(t for t in account.values() if t["mapping"] == "COUNT(*)")
        assert count["term"] == "银行账户总数"
        assert count["aliases"] == ["银行账户数量", "银行账户记录数"]
        assert count["tables"] == ["account"]

    def test_sum_and_avg_terms_for_described_numeric_columns(self):
        terms = generate_terms(TABLES)
        loan = {t["term"]: t for t in terms if "loan" in t["tables"]}
        assert loan["贷款总金额"]["mapping"] == "SUM(amount)"
        assert loan["贷款平均金额"]["mapping"] == "AVG(amount)"
        assert loan["贷款平均期限"]["mapping"] == "AVG(duration)"

    def test_date_column_gets_avg_year_term(self):
        terms = generate_terms(TABLES)
        account = {t["term"]: t for t in terms if "account" in t["tables"]}
        date_terms = [t for t in account.values() if "日期" in t["term"]]
        assert date_terms
        assert all(t["mapping"] == "AVG(EXTRACT(YEAR FROM date))" for t in date_terms)

    def test_undescribed_columns_get_no_terms(self):
        """无描述的列(如 A1~A16)不生成 term——名字无从可靠推导。"""
        tables = [{
            "name": "district",
            "description": "地区信息表，包含地区ID及人口、收入等统计数据",
            "columns": [
                {"name": "district_id", "type": "int", "description": "地区唯一标识符", "enums": []},
                {"name": "A11", "type": "int", "description": "", "enums": []},
            ],
            "metrics": [],
        }]
        terms = generate_terms(tables)
        assert len(terms) == 1  # 只有 count term
        assert terms[0]["mapping"] == "COUNT(*)"

    def test_id_columns_get_no_sum_terms(self):
        """ID 列不生成 SUM/AVG term(对 ID 求平均没有业务含义)。"""
        terms = generate_terms(TABLES)
        all_terms = [t["term"] for t in terms]
        assert not any("account_id" in t or "唯一标识符" in t for t in all_terms)
        assert not any(t["mapping"] == "SUM(account_id)" for t in terms)

    def test_term_definitions_carry_table_name(self):
        terms = generate_terms(TABLES)
        loan_count = next(
            t for t in terms if t["mapping"] == "COUNT(*)" and "loan" in t["tables"]
        )
        assert "贷款" in loan_count["definition"]


class TestGenerateTemplates:
    def test_count_template_per_table(self):
        templates = generate_templates(TABLES)
        account = next(t for t in templates if "account" in t["sql"])
        assert account["template"] is True
        assert account["sql"] == "SELECT COUNT(*) FROM account"
        assert account["question"]

    def test_group_by_template_over_first_text_column(self):
        templates = generate_templates(TABLES)
        account = next(
            t for t in templates
            if t["sql"].startswith("SELECT frequency")
        )
        assert account["sql"] == "SELECT frequency, COUNT(*) FROM account GROUP BY frequency"

    def test_group_by_template_over_first_text_column_loan(self):
        templates = generate_templates(TABLES)
        loan = next(
            t for t in templates
            if t["sql"].startswith("SELECT status")
        )
        assert loan["sql"] == "SELECT status, COUNT(*) FROM loan GROUP BY status"

    def test_tables_without_text_columns_get_count_only(self):
        tables = [{
            "name": "t",
            "description": "只有数值列的表",
            "columns": [{"name": "n", "type": "int", "description": "数值", "enums": []}],
            "metrics": [],
        }]
        templates = generate_templates(tables)
        assert len(templates) == 1
        assert templates[0]["sql"] == "SELECT COUNT(*) FROM t"
