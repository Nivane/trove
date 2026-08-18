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
            {"name": "account_id", "type": "int", "description": "贷款关联账户ID", "enums": []},
            {"name": "amount", "type": "int", "description": "贷款金额", "enums": []},
            {"name": "duration", "type": "int", "description": "贷款期限（月数）", "enums": []},
            {"name": "status", "type": "varchar", "description": "贷款状态",
             "enums": ["A=contract finished", "B=contract running", "C=contract finished loan not paid"]},
        ],
        "metrics": [],
    },
    {
        "name": "district",
        "description": "地区信息表，包含地区ID、名称和人口统计",
        "columns": [
            {"name": "district_id", "type": "int", "description": "地区唯一标识符", "enums": []},
            {"name": "name", "type": "varchar", "description": "地区名称", "enums": []},
        ],
        "metrics": [],
    },
]


class TestGenerateTerms:
    def test_count_term_per_table_with_aliases(self):
        terms = generate_terms(TABLES, lang="zh")
        account = {t["term"]: t for t in terms if "account" in t["tables"]}
        count = next(t for t in account.values() if t["mapping"] == "COUNT(*)")
        assert count["term"] == "银行账户总数"
        assert count["aliases"] == ["银行账户数量", "银行账户记录数"]
        assert count["tables"] == ["account"]

    def test_sum_and_avg_terms_for_described_numeric_columns(self):
        terms = generate_terms(TABLES, lang="zh")
        loan = {t["term"]: t for t in terms if "loan" in t["tables"]}
        assert loan["贷款总金额"]["mapping"] == "SUM(amount)"
        assert loan["贷款平均金额"]["mapping"] == "AVG(amount)"
        assert loan["贷款平均期限"]["mapping"] == "AVG(duration)"

    def test_date_column_gets_avg_year_term(self):
        terms = generate_terms(TABLES, lang="zh")
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
        terms = generate_terms(tables, lang="zh")
        assert len(terms) == 1  # 只有 count term
        assert terms[0]["mapping"] == "COUNT(*)"

    def test_id_columns_get_no_sum_terms(self):
        """ID 列不生成 SUM/AVG term(对 ID 求平均没有业务含义)。"""
        terms = generate_terms(TABLES, lang="zh")
        all_terms = [t["term"] for t in terms]
        assert not any("account_id" in t or "唯一标识符" in t for t in all_terms)
        assert not any(t["mapping"] == "SUM(account_id)" for t in terms)

    def test_term_definitions_carry_table_name(self):
        terms = generate_terms(TABLES, lang="zh")
        loan_count = next(
            t for t in terms if t["mapping"] == "COUNT(*)" and "loan" in t["tables"]
        )
        assert "贷款" in loan_count["definition"]


class TestGenerateTemplates:
    def test_count_template_per_table(self):
        templates = generate_templates(TABLES, lang="zh")
        account = next(t for t in templates if "account" in t["sql"])
        assert account["template"] is True
        assert account["sql"] == "SELECT COUNT(*) FROM account"
        assert account["question"]

    def test_group_by_template_over_first_text_column(self):
        templates = generate_templates(TABLES, lang="zh")
        account = next(
            t for t in templates
            if t["sql"].startswith("SELECT frequency")
        )
        assert account["sql"] == "SELECT frequency, COUNT(*) FROM account GROUP BY frequency"

    def test_group_by_template_over_first_text_column_loan(self):
        templates = generate_templates(TABLES, lang="zh")
        loan = next(
            t for t in templates
            if t["sql"].startswith("SELECT status")
        )
        assert loan["sql"] == "SELECT status, COUNT(*) FROM loan GROUP BY status"

    def test_tables_without_text_columns_get_count_only(self):
        """无文本列的表:COUNT + 数值列聚合/比较模板(D 族)。"""
        tables = [{
            "name": "t",
            "description": "只有数值列的表",
            "columns": [{"name": "n", "type": "int", "description": "数值", "enums": []}],
            "metrics": [],
        }]
        templates = generate_templates(tables, lang="zh")
        sqls = [t["sql"] for t in templates]
        assert sqls.count("SELECT COUNT(*) FROM t") == 1
        assert "SELECT MAX(n) FROM t" in sqls
        assert "SELECT AVG(n) FROM t" in sqls
        assert "SELECT COUNT(*) FROM t WHERE n > 0" in sqls

    # —— 组合模板:JOIN 骨架 + WHERE 过滤 ——

    def test_join_template_from_same_name_fk(self):
        """account.district_id + district.district_id 同名键 → JOIN 模板。"""
        templates = generate_templates(TABLES, lang="zh")
        join = next(
            t for t in templates
            if t["sql"] == "SELECT COUNT(*) FROM account JOIN district ON account.district_id = district.district_id"
        )
        assert join["question"] == "地区中有多少银行账户记录？"
        assert set(join["tags"]) == {"银行账户", "地区", "连接", "聚合"}

    def test_join_group_template_over_dimension_text_column(self):
        """JOIN + 维度表文本列分组(地区名称分组)。"""
        templates = generate_templates(TABLES, lang="zh")
        grouped = next(
            t for t in templates
            if t["sql"].startswith("SELECT district.name")
        )
        assert grouped["sql"] == (
            "SELECT district.name, COUNT(*) FROM account JOIN district "
            "ON account.district_id = district.district_id GROUP BY district.name"
        )
        assert grouped["question"] == "按地区的地区名称分组，统计每种地区名称的银行账户数量"

    def test_enum_filter_template_takes_sample_values(self):
        """enum 列取前 3 个值生成 WHERE 过滤模板。"""
        templates = generate_templates(TABLES, lang="zh")
        filters = [t for t in templates if t["sql"].startswith("SELECT COUNT(*) FROM loan WHERE status")]
        assert len(filters) == 3
        assert filters[0]["sql"] == "SELECT COUNT(*) FROM loan WHERE status = 'A'"
        assert filters[1]["sql"] == "SELECT COUNT(*) FROM loan WHERE status = 'B'"
        assert filters[0]["question"] == "贷款中贷款状态为'A'的记录有多少？"
        assert "过滤" in filters[0]["tags"]

    def test_primary_key_does_not_create_self_join(self):
        """{table}_id 主键不是 FK → 不生成自连接模板。"""
        templates = generate_templates(TABLES, lang="zh")
        self_joins = [t for t in templates if "ON account.account_id" in t["sql"]]
        assert self_joins == []

    def test_numeric_columns_get_aggregate_and_gt0_templates(self):
        """数值列(贷款金额/期限)生成 MAX/MIN/AVG/SUM + >0 比较模板。"""
        templates = generate_templates(TABLES, lang="zh")
        sqls = [t["sql"] for t in templates]
        for fn in ("MAX", "MIN", "AVG", "SUM"):
            assert f"SELECT {fn}(amount) FROM loan" in sqls, fn
            assert f"SELECT {fn}(duration) FROM loan" in sqls, fn
        assert "SELECT COUNT(*) FROM loan WHERE amount > 0" in sqls
        assert "SELECT COUNT(*) FROM loan WHERE duration > 0" in sqls

    def test_numeric_templates_carry_business_questions(self):
        """问题文本带业务描述(描述权威,非列名)。"""
        templates = generate_templates(TABLES, lang="zh")
        by_sql = {t["sql"]: t for t in templates}
        avg = by_sql["SELECT AVG(amount) FROM loan"]
        assert avg["question"] == "贷款金额的平均值是多少？"
        assert avg["tags"] == ["贷款", "amount", "聚合"]
        gt0 = by_sql["SELECT COUNT(*) FROM loan WHERE amount > 0"]
        assert gt0["question"] == "贷款中贷款金额大于 0 的记录有多少？"

    def test_date_column_gets_earliest_latest_templates(self):
        """日期列生成 MIN/MAX(最早/最晚)模板;不做等值/区间(需样例值)。"""
        templates = generate_templates(TABLES, lang="zh")
        sqls = [t["sql"] for t in templates]
        assert "SELECT MIN(date) FROM account" in sqls
        assert "SELECT MAX(date) FROM account" in sqls
        assert "WHERE date" not in " ".join(sqls)

    def test_id_columns_get_no_numeric_templates(self):
        """ID 列(account_id/loan_id)不对聚合/比较求值。"""
        templates = generate_templates(TABLES, lang="zh")
        sqls = [t["sql"] for t in templates]
        assert not any("MAX(account_id)" in s or "AVG(loan_id)" in s for s in sqls)
        assert not any("WHERE account_id" in s or "WHERE loan_id" in s for s in sqls)

    def test_undescribed_numeric_column_skipped(self):
        """无描述数值列(A11 类)不生成模板——名字无从可靠推导。"""
        tables = [{
            "name": "district",
            "description": "地区信息表",
            "columns": [
                {"name": "district_id", "type": "int", "description": "地区唯一标识符", "enums": []},
                {"name": "A11", "type": "int", "description": "", "enums": []},
                {"name": "A12", "type": "double", "description": "失业率 1995", "enums": []},
            ],
            "metrics": [],
        }]
        templates = generate_templates(tables, lang="zh")
        sqls = [t["sql"] for t in templates]
        assert not any("A11" in s for s in sqls)
        assert "SELECT MAX(A12) FROM district" in sqls  # 有描述才生成


EN_TABLES = [
    {
        "name": "account",
        "description": "bank account information",
        "columns": [
            {"name": "account_id", "type": "int", "description": "account identifier", "enums": []},
            {"name": "district_id", "type": "int", "description": "district of the account", "enums": []},
            {"name": "frequency", "type": "varchar", "description": "statement issuance frequency", "enums": []},
            {"name": "date", "type": "date", "description": "account opening date", "enums": []},
        ],
        "metrics": [],
    },
    {
        "name": "loan",
        "description": "loan information",
        "columns": [
            {"name": "loan_id", "type": "int", "description": "loan identifier", "enums": []},
            {"name": "amount", "type": "int", "description": "loan amount", "enums": []},
            {"name": "status", "type": "varchar", "description": "loan status",
             "enums": ["A=contract finished", "B=contract running"]},
        ],
        "metrics": [],
    },
    {
        "name": "district",
        "description": "geographic district",
        "columns": [
            {"name": "district_id", "type": "int", "description": "district identifier", "enums": []},
            {"name": "name", "type": "varchar", "description": "district name", "enums": []},
        ],
        "metrics": [],
    },
    {
        "name": "order",
        "description": "payment order",
        "columns": [
            {"name": "order_id", "type": "int", "description": "order identifier", "enums": []},
            {"name": "account_to", "type": "int", "description": "recipient account number", "enums": []},
        ],
        "metrics": [],
    },
]


class TestEnglishGeneration:
    """默认 lang="en":面向英文 benchmark 的术语/模板;中文描述列跳过派生。"""

    def test_count_term_in_english(self):
        terms = generate_terms(EN_TABLES)
        loan = [t for t in terms if t["tables"] == ["loan"] and t["mapping"] == "COUNT(*)"]
        assert loan and loan[0]["term"] == "number of loan records"

    def test_sum_avg_terms_from_english_descriptions(self):
        terms = generate_terms(EN_TABLES)
        loan = {t["term"]: t for t in terms if "loan" in t["tables"]}
        assert loan["total loan amount"]["mapping"] == "SUM(amount)"
        assert loan["average loan amount"]["mapping"] == "AVG(amount)"

    def test_cjk_descriptions_skipped_in_english_mode(self):
        """中文描述无法确定性翻译成英文 → 与无描述列同等待遇,不生成术语。"""
        tables = [{
            "name": "loan",
            "description": "贷款信息表",
            "columns": [
                {"name": "amount", "type": "int", "description": "贷款金额", "enums": []},
            ],
            "metrics": [],
        }]
        terms = generate_terms(tables)
        assert [t for t in terms if t["mapping"] != "COUNT(*)"] == []

    def test_account_number_column_gets_no_sum_terms_in_either_lang(self):
        """account_to 不是 _id 后缀但同为标识类(账户号)→ 两种语言都不生成 SUM/AVG。"""
        for lang in ("en", "zh"):
            terms = generate_terms(EN_TABLES, lang=lang)
            assert not any(
                t["mapping"].startswith(("SUM(account_to", "AVG(account_to"))
                for t in terms
            ), f"{lang}: account_to 不应生成 SUM/AVG"

    def test_count_template_in_english(self):
        templates = generate_templates(EN_TABLES)
        account = next(
            t for t in templates
            if t["sql"] == "SELECT COUNT(*) FROM account"
        )
        assert account["question"] == "How many records are in the account table?"

    def test_group_by_template_in_english(self):
        templates = generate_templates(EN_TABLES)
        loan = next(t for t in templates if t["sql"].startswith("SELECT status"))
        assert loan["question"] == "How many loan records are there for each loan status?"

    def test_join_and_join_group_templates_in_english(self):
        templates = generate_templates(EN_TABLES)
        join = next(
            t for t in templates
            if t["sql"] == "SELECT COUNT(*) FROM account JOIN district ON account.district_id = district.district_id"
        )
        assert join["question"] == "How many account records are there in district?"
        assert set(join["tags"]) == {"account", "district", "join", "aggregation"}
        grouped = next(
            t for t in templates
            if t["sql"].startswith("SELECT district.name")
        )
        assert grouped["question"] == (
            "How many account records are there for each district name of district?"
        )

    def test_enum_filter_templates_in_english(self):
        """问题文本用人类可读 label(male/female),SQL 保留 code 值。"""
        templates = generate_templates(EN_TABLES)
        filters = [t for t in templates if t["sql"].startswith("SELECT COUNT(*) FROM loan WHERE status")]
        assert [t["sql"] for t in filters] == [
            "SELECT COUNT(*) FROM loan WHERE status = 'A'",
            "SELECT COUNT(*) FROM loan WHERE status = 'B'",
        ]
        assert filters[0]["question"] == (
            "How many loan records are contract finished?")

    def test_multiline_enum_generates_all_values_with_labels(self):
        """多行 enum('F=female\\nM=male')逐行解析,label 进问题文本。"""
        tables = [{
            "name": "client",
            "description": "client information",
            "columns": [
                {"name": "client_id", "type": "int", "description": "client identifier", "enums": []},
                {"name": "gender", "type": "text", "description": "gender of client",
                 "enums": ["F=female\nM=male"]},
            ],
            "metrics": [],
        }]
        templates = generate_templates(tables)
        filters = [t for t in templates if t["sql"].startswith("SELECT COUNT(*) FROM client WHERE gender")]
        assert [(t["sql"], t["question"]) for t in filters] == [
            ("SELECT COUNT(*) FROM client WHERE gender = 'F'",
             "How many client records are female?"),
            ("SELECT COUNT(*) FROM client WHERE gender = 'M'",
             "How many client records are male?"),
        ]

    def test_stands_for_enum_format_parsed_cleanly(self):
        """''A' stands for contract finished' → code='A', label 去 stands for。"""
        tables = [{
            "name": "loan",
            "description": "loan information",
            "columns": [
                {"name": "status", "type": "text", "description": "loan status",
                 "enums": ["'A' stands for contract finished;\n'B' stands for running"]},
            ],
            "metrics": [],
        }]
        templates = generate_templates(tables)
        filters = [t for t in templates if t["sql"].startswith("SELECT COUNT(*) FROM loan WHERE status")]
        assert filters[0]["sql"] == "SELECT COUNT(*) FROM loan WHERE status = 'A'"
        assert "stand for" not in filters[0]["sql"]
        assert filters[0]["question"].endswith("are contract finished?")

    def test_narrative_enum_lines_skipped(self):
        """叙述行('each bank has unique two-letter code')不当成值。"""
        tables = [{
            "name": "trans",
            "description": "transaction information",
            "columns": [
                {"name": "bank", "type": "text", "description": "bank of the partner",
                 "enums": ["each bank has unique two-letter code", "AB", "YZ"]},
            ],
            "metrics": [],
        }]
        templates = generate_templates(tables)
        filters = [t for t in templates if t["sql"].startswith("SELECT COUNT(*) FROM trans WHERE bank")]
        sqls = [t["sql"] for t in filters]
        assert "each bank" not in " ".join(sqls)
        assert sqls == [
            "SELECT COUNT(*) FROM trans WHERE bank = 'AB'",
            "SELECT COUNT(*) FROM trans WHERE bank = 'YZ'",
        ]

    def test_numeric_templates_in_english(self):
        """数值列聚合/比较模板问题文本用英文描述(检索锚:business 词)。"""
        templates = generate_templates(EN_TABLES)
        by_sql = {t["sql"]: t for t in templates}
        assert by_sql["SELECT MAX(amount) FROM loan"]["question"] == (
            "What is the maximum loan amount?")
        assert by_sql["SELECT AVG(amount) FROM loan"]["question"] == (
            "What is the average loan amount?")
        assert by_sql["SELECT SUM(amount) FROM loan"]["question"] == (
            "What is the total loan amount?")
        assert by_sql["SELECT COUNT(*) FROM loan WHERE amount > 0"]["question"] == (
            "How many loan records have loan amount greater than 0?")
        assert by_sql["SELECT MAX(amount) FROM loan"]["tags"] == [
            "loan", "amount", "aggregation"]

    def test_date_templates_in_english(self):
        templates = generate_templates(EN_TABLES)
        by_sql = {t["sql"]: t for t in templates}
        assert by_sql["SELECT MIN(date) FROM account"]["question"] == (
            "What is the earliest account opening date?")
        assert by_sql["SELECT MAX(date) FROM account"]["question"] == (
            "What is the latest account opening date?")

    def test_account_number_column_gets_no_numeric_templates(self):
        """account_to(账户号,标识类)不生成数值聚合/比较模板。"""
        templates = generate_templates(EN_TABLES)
        sqls = [t["sql"] for t in templates]
        assert not any("account_to" in s for s in sqls)
