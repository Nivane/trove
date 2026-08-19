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
        count = next(t for t in account.values() if t["mapping"] == "COUNT(account.account_id)")
        assert count["term"] == "银行账户总数"
        assert count["aliases"] == ["银行账户数量", "银行账户记录数"]
        assert count["tables"] == ["account"]

    def test_sum_and_avg_terms_for_described_numeric_columns(self):
        terms = generate_terms(TABLES, lang="zh")
        loan = {t["term"]: t for t in terms if "loan" in t["tables"]}
        assert loan["贷款总金额"]["mapping"] == "SUM(loan.amount)"
        assert loan["贷款平均金额"]["mapping"] == "AVG(loan.amount)"
        assert loan["贷款平均期限"]["mapping"] == "AVG(loan.duration)"

    def test_date_column_gets_avg_year_term(self):
        terms = generate_terms(TABLES, lang="zh")
        account = {t["term"]: t for t in terms if "account" in t["tables"]}
        date_terms = [t for t in account.values() if "日期" in t["term"]]
        assert date_terms
        assert all(t["mapping"] == "AVG(EXTRACT(YEAR FROM account.date))"
                   for t in date_terms)

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
        assert terms[0]["mapping"] == "COUNT(district.district_id)"

    def test_id_columns_get_no_sum_terms(self):
        """ID 列不生成 SUM/AVG term(对 ID 求平均没有业务含义)。"""
        terms = generate_terms(TABLES, lang="zh")
        all_terms = [t["term"] for t in terms]
        assert not any("account_id" in t or "唯一标识符" in t for t in all_terms)
        assert not any(t["mapping"] == "SUM(account.account_id)" for t in terms)

    def test_term_definitions_carry_table_name(self):
        terms = generate_terms(TABLES, lang="zh")
        loan_count = next(
            t for t in terms if t["mapping"] == "COUNT(loan.loan_id)" and "loan" in t["tables"]
        )
        assert "贷款" in loan_count["definition"]

    def test_count_id_selection_prefers_primary_key(self):
        """primary_key 列优先于更靠前的 id 命名列(id 列非空由主键保证)。"""
        tables = [{
            "name": "payments",
            "columns": [
                {"name": "seq_no", "type": "int", "description": "流水号", "primary_key": False},
                {"name": "pay_id", "type": "int", "description": "支付ID",
                 "primary_key": True},
            ],
        }]
        terms = generate_terms(tables, lang="zh")
        assert terms[0]["mapping"] == "COUNT(payments.pay_id)"

    def test_count_id_selection_falls_back_to_first_column(self):
        """无主键也无 id 命名列 → 首列(确定性回退)。"""
        tables = [{
            "name": "events",
            "columns": [
                {"name": "ts", "type": "timestamp", "description": "事件时间"},
                {"name": "kind", "type": "varchar", "description": "事件类型"},
            ],
        }]
        terms = generate_terms(tables, lang="zh")
        assert terms[0]["mapping"] == "COUNT(events.ts)"

    def test_count_id_selection_zero_columns_keeps_count_star(self):
        """无列可依 → COUNT(*)(不做无依据假设;此时无锚定是唯一选择)。"""
        terms = generate_terms([{"name": "ghost", "columns": []}], lang="zh")
        assert terms[0]["mapping"] == "COUNT(*)"


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
        loan = [t for t in terms if t["tables"] == ["loan"]
                and t["mapping"] == "COUNT(loan.loan_id)"]
        assert loan and loan[0]["term"] == "number of loan records"

    def test_sum_avg_terms_from_english_descriptions(self):
        terms = generate_terms(EN_TABLES)
        loan = {t["term"]: t for t in terms if "loan" in t["tables"]}
        assert loan["total loan amount"]["mapping"] == "SUM(loan.amount)"
        assert loan["average loan amount"]["mapping"] == "AVG(loan.amount)"

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
        # 唯一非 count term 判定:首列回退 → COUNT(loan.amount)
        assert [t for t in terms if t["mapping"] != "COUNT(loan.amount)"] == []

    def test_account_number_column_gets_no_sum_terms_in_either_lang(self):
        """account_to 不是 _id 后缀但同为标识类(账户号)→ 两种语言都不生成 SUM/AVG。"""
        for lang in ("en", "zh"):
            terms = generate_terms(EN_TABLES, lang=lang)
            assert not any(
                t["mapping"].startswith(('SUM("order".account_to', 'AVG("order".account_to'))
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


DATE_TABLES = [
    {
        "name": "account",
        "description": "Bank account",
        "columns": [
            {"name": "account_id", "type": "int", "description": "id", "enums": []},
            {"name": "date", "type": "date", "description": "account opening date",
             "enums": [], "range": ["930101", "971231"]},
        ],
        "metrics": [],
    },
    {
        "name": "client",
        "description": "Client",
        "columns": [
            {"name": "client_id", "type": "int", "description": "id", "enums": []},
            {"name": "birth_date", "type": "date", "description": "birth date",
             "enums": [], "range": ["200101", "951231"]},
        ],
        "metrics": [],
    },
]


class TestDateRangeTemplates:
    def _sqls(self, tables):
        return {t["sql"]: t for t in generate_templates(tables)}

    def test_year_equality_templates_for_each_year(self):
        """range 内每年一个年份等值模板(YYYYMMDD 前缀比较,问题文本含 4 位年)。"""
        by_sql = self._sqls(DATE_TABLES)
        assert by_sql["SELECT COUNT(*) FROM account WHERE substr(date, 1, 2) = '93'"] is not None
        assert by_sql["SELECT COUNT(*) FROM account WHERE substr(date, 1, 2) = '97'"] is not None
        assert by_sql["SELECT COUNT(*) FROM account WHERE substr(date, 1, 2) = '97'"]["question"] == (
            "How many account records have account opening date in 1997?")
        assert not any("substr(date, 1, 2) = '98'" in s for s in by_sql)

    def test_full_range_between_template(self):
        by_sql = self._sqls(DATE_TABLES)
        t = by_sql["SELECT COUNT(*) FROM account WHERE date BETWEEN '930101' AND '971231'"]
        assert t["question"] == (
            "How many account records have account opening date between 1993 and 1997?")

    def test_endpoint_equal_templates(self):
        """值域端点等值模板:SQL 用存储格式,问题文本用人类可读日期。"""
        by_sql = self._sqls(DATE_TABLES)
        t = by_sql["SELECT COUNT(*) FROM account WHERE date = '930101'"]
        assert t["question"] == (
            "How many account records have account opening date on 1993-01-01?")
        assert by_sql["SELECT COUNT(*) FROM account WHERE date = '971231'"] is not None

    def test_before_after_templates(self):
        """before/after 用年份前缀比较(不含边界年本身)。"""
        by_sql = self._sqls(DATE_TABLES)
        assert by_sql["SELECT COUNT(*) FROM account WHERE substr(date, 1, 2) < '97'"] is not None
        assert by_sql["SELECT COUNT(*) FROM account WHERE substr(date, 1, 2) > '93'"] is not None
        assert by_sql["SELECT COUNT(*) FROM account WHERE substr(date, 1, 2) < '97'"]["question"] == (
            "How many account records have account opening date before 1997?")
        assert by_sql["SELECT COUNT(*) FROM account WHERE substr(date, 1, 2) > '93'"]["question"] == (
            "How many account records have account opening date after 1993?")

    def test_standard_ymd_format_range(self):
        """YYYY-MM-DD range:前缀 4 位比较,区间字面量原样。"""
        tables = [{
            "name": "event", "description": "Event",
            "columns": [{"name": "happened_on", "type": "date",
                         "description": "event date", "enums": [],
                         "range": ["1995-01-01", "1998-12-31"]}],
        }]
        by_sql = self._sqls(tables)
        assert by_sql["SELECT COUNT(*) FROM event WHERE substr(happened_on, 1, 4) = '1995'"] is not None
        assert by_sql["SELECT COUNT(*) FROM event WHERE substr(happened_on, 1, 4) = '1998'"] is not None
        t = by_sql["SELECT COUNT(*) FROM event WHERE happened_on BETWEEN '1995-01-01' AND '1998-12-31'"]
        assert t["question"] == (
            "How many event records have event date between 1995 and 1998?")
        assert by_sql["SELECT COUNT(*) FROM event WHERE happened_on = '1998-12-31'"]["question"] == (
            "How many event records have event date on 1998-12-31?")

    def test_long_span_sampled_by_decade(self):
        """跨度 > cap(如出生日期 1920-1995):每 decade 一个代表 + 端点,不超 14 个。"""
        by_sql = self._sqls(DATE_TABLES)
        year_questions = {
            t["question"] for t in by_sql.values()
            if "birth date in " in t["question"]
        }
        assert "How many client records have birth date in 1920?" in year_questions
        assert "How many client records have birth date in 1930?" in year_questions
        assert "How many client records have birth date in 1990?" in year_questions
        assert "How many client records have birth date in 1995?" in year_questions
        assert "How many client records have birth date in 1921?" not in year_questions
        assert len(year_questions) <= 14

    def test_no_range_keeps_only_min_max(self):
        """无 range 字段 → 向后兼容:只有 MIN/MAX 聚合模板,无区间/年份模板。"""
        tables = [{
            "name": "account", "description": "Bank account",
            "columns": [{"name": "date", "type": "date",
                         "description": "account opening date", "enums": []}],
        }]
        sqls = [t["sql"] for t in generate_templates(tables)]
        assert "SELECT MIN(date) FROM account" in sqls
        assert not any("WHERE" in s for s in sqls)

    def test_invalid_or_reversed_range_ignored(self):
        for bad_range in (["abc", "xyz"], ["971231", "930101"], ["93"], []):
            tables = [{
                "name": "account", "description": "Bank account",
                "columns": [{"name": "date", "type": "date",
                             "description": "account opening date", "enums": [],
                             "range": bad_range}],
            }]
            sqls = [t["sql"] for t in generate_templates(tables)]
            assert not any("WHERE" in s for s in sqls), bad_range

    def test_chinese_question_shapes(self):
        templates = generate_templates(DATE_TABLES, lang="zh")
        zh = {t["question"]: t["sql"] for t in templates}
        assert "Bank中account opening date在1993年的记录有多少？" in zh
        assert zh["Bank中account opening date在1993年的记录有多少？"] == (
            "SELECT COUNT(*) FROM account WHERE substr(date, 1, 2) = '93'")
        assert "Bank中account opening date在1993到1997年之间的记录有多少？" in zh
        assert "Bank中account opening date早于1997年的记录有多少？" in zh
