"""KB lint tests — 静态检查 trove KB 的已知劣化模式.

覆盖 kb init 确定性生成暴露过的缺陷:
  - 术语 mapping 引用不存在的列
  - 对 ID/账号类列求 SUM/AVG(如 SUM(account_to))
  - 示例 SQL 无法解析或引用不存在的表
  - 列描述留空(如 district A 列 description: '')
  - lessons pattern 过长/note 为空
  - 纯中文示例对英文问题检索不可达
"""

from trove.services.kb.lint import (
    lint_examples,
    lint_lessons,
    lint_semantics,
    lint_terms,
    lint_tables,
    parse_enum_values,
)

SCHEMA = {
    "loan": {"loan_id", "amount", "duration", "status"},
    "order": {"order_id", "account_to", "amount"},
    "district": {"A2", "A11"},
}


class TestLintTerms:
    def test_unknown_column_flagged(self):
        issues = lint_terms(
            [{"term": "x", "mapping": "SUM(nope)", "tables": ["loan"],
              "aliases": [], "definition": ""}],
            SCHEMA,
        )
        assert any("nope" in i for i in issues)

    def test_table_qualified_column_checked_against_that_table(self):
        issues = lint_terms(
            [{"term": "x", "mapping": "AVG(loan.status)", "tables": ["loan"],
              "aliases": [], "definition": ""}],
            SCHEMA,
        )
        # status 存在但是文本列 → 只查列存在性,类型另由 type 检查负责
        assert issues == []

    def test_id_like_sum_avg_flagged(self):
        issues = lint_terms(
            [{"term": "总收款方账户号", "mapping": "SUM(account_to)",
              "tables": ["order"], "aliases": [], "definition": ""}],
            SCHEMA,
        )
        assert any("account_to" in i for i in issues)

    def test_clean_term_passes(self):
        issues = lint_terms(
            [{"term": "贷款总金额", "mapping": "SUM(amount)", "tables": ["loan"],
              "aliases": [], "definition": ""}],
            SCHEMA,
        )
        assert issues == []

    def test_column_match_is_case_insensitive(self):
        """schema 列名大写(A5)与 mapping 小写(a5)应视为同一列。"""
        schema = {"district": {"A5", "A6"}}
        issues = lint_terms(
            [{"term": "地区平均人口", "mapping": "AVG(a5)", "tables": ["district"],
              "aliases": [], "definition": ""}],
            schema,
        )
        assert issues == []


class TestLintExamples:
    def test_unparseable_sql_flagged(self):
        issues = lint_examples(
            [{"question": "q", "sql": "SELEC broken", "tags": []}],
            set(SCHEMA),
        )
        assert any("解析" in i for i in issues)

    def test_unknown_table_flagged(self):
        issues = lint_examples(
            [{"question": "q", "sql": "SELECT * FROM missing_table", "tags": []}],
            set(SCHEMA),
        )
        assert any("missing_table" in i for i in issues)

    def test_write_sql_flagged(self):
        issues = lint_examples(
            [{"question": "q", "sql": "DELETE FROM loan", "tags": []}],
            set(SCHEMA),
        )
        assert any("写操作" in i for i in issues)

    def test_pure_chinese_question_warned(self):
        issues = lint_examples(
            [{"question": "客户银行账户表中有多少条记录？",
              "sql": "SELECT COUNT(*) FROM loan", "tags": ["账户"]}],
            set(SCHEMA),
        )
        assert any("英文" in i for i in issues)

    def test_clean_example_passes(self):
        issues = lint_examples(
            [{"question": "how many loans are running",
              "sql": "SELECT COUNT(*) FROM loan", "tags": ["loan"]}],
            set(SCHEMA),
        )
        assert issues == []

    def test_mysql_dialect_sql_parses(self):
        """BIRD gold 的 MySQL 写法(反引号、CAST AS DOUBLE、DATE_FORMAT)可解析。"""
        issues = lint_examples(
            [{"question": "growth rate question", "tags": ["growth"],
              "sql": "SELECT CAST(SUM(CASE WHEN DATE_FORMAT(CAST(`T1`.`date` AS DATETIME), '%Y') = '1997' THEN `T1`.`amount` ELSE 0 END) AS DOUBLE) * 100 FROM `loan` AS `T1`"}],
            set(SCHEMA),
        )
        assert issues == []


class TestLintTables:
    def test_empty_column_descriptions_warned(self):
        issues = lint_tables([
            {"name": "district", "description": "地区表", "columns": {
                "A2": "地区名称",
                "A11": "",
            }},
        ])
        assert any("A11" in i for i in issues)

    def test_described_columns_pass(self):
        issues = lint_tables([
            {"name": "district", "description": "地区表", "columns": {
                "A2": "地区名称",
            }},
        ])
        assert issues == []


class TestLintLessons:
    def test_overlong_pattern_flagged(self):
        issues = lint_lessons([
            {"pattern": "p" * 41, "note": "ok", "confirmed": True},
        ])
        assert any("pattern" in i for i in issues)

    def test_empty_note_flagged(self):
        issues = lint_lessons([
            {"pattern": "short", "note": "", "confirmed": True},
        ])
        assert any("note" in i for i in issues)

    def test_clean_lesson_passes(self):
        issues = lint_lessons([
            {"pattern": "weekly statements", "note": "用 frequency 过滤", "confirmed": True},
        ])
        assert issues == []


class TestParseEnumValues:
    def test_eq_format(self):
        assert parse_enum_values("A=合同已结清; B=合同结束") == {"A", "B"}

    def test_bird_quoted_format(self):
        text = ("'A' stands for contract finished, no problems;\n"
                "'B' stands for running contract, client in debt")
        assert parse_enum_values(text) == {"A", "B"}

    def test_raw_probe_values(self):
        assert parse_enum_values("POPLATEK MESICNE; POPLATEK TYDNE") == {
            "POPLATEK MESICNE", "POPLATEK TYDNE"}

    def test_full_width_colon_format(self):
        assert parse_enum_values("F：female\nM：male") == {"F", "M"}


class TestLintSemantics:
    def _model(self):
        return {
            "name": "fin",
            "datasets": [
                {"name": "district", "fields": [
                    {"name": "A3", "expression": {"dialects": [
                        {"dialect": "ANSI_SQL", "expression": "A3"}]}},
                    {"name": "A3", "expression": {"dialects": [
                        {"dialect": "ANSI_SQL", "expression": "A3"}]}},
                    {"name": "A11", "expression": {"dialects": [
                        {"dialect": "ANSI_SQL", "expression": "A11"}]},
                     "ai_context": {"synonyms": ["salary", ""]}},
                ]},
            ],
            "relationships": [{"name": "r", "from": "ghost", "to": "district"}],
            "metrics": [
                {"name": "m", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "SUM(("}]}},
                {"name": "m", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "SUM(loan.amount)"}]}},
            ],
        }

    def test_duplicate_field_and_bad_alias_flagged(self):
        issues = lint_semantics(self._model())
        assert any("字段「A3」重复" in i for i in issues)
        assert any("空/非法 synonym" in i for i in issues)

    def test_duplicate_metric_and_unparseable_expr_flagged(self):
        issues = lint_semantics(self._model())
        assert any("指标「m」重复" in i for i in issues)
        assert any("表达式无法解析" in i for i in issues)

    def test_relationship_to_undeclared_dataset_flagged(self):
        issues = lint_semantics(self._model())
        assert any("引用未声明的数据集" in i for i in issues)

    def test_relationship_without_cardinality_flagged(self):
        """P0-3:未声明基数 → 编译器保守 MISS,lint 在建模期暴露。"""
        model = {
            "datasets": [{"name": "loan"}, {"name": "account"}],
            "relationships": [{"name": "r", "from": "loan", "to": "account",
                               "from_columns": ["account_id"], "to_columns": ["account_id"]}],
            "metrics": [],
        }
        issues = lint_semantics(model)
        assert any("未声明基数" in i for i in issues)

    def test_missing_relationship_by_naming_convention_flagged(self):
        """P2:命名约定 FK 指向已声明表但未声明关系 → 建模期警告。"""
        model = {
            "datasets": [{"name": "loan", "fields": [
                {"name": "account_id", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "account_id"}]}},
            ]}, {"name": "account"}],
            "relationships": [],
            "metrics": [],
        }
        issues = lint_semantics(model)
        assert any("但 relationships 未声明这对关系" in i for i in issues)

    def test_metric_filter_undeclared_column_flagged(self):
        model = {
            "datasets": [{"name": "loan", "fields": [{"name": "loan_id"}, {"name": "status"}]}],
            "relationships": [],
            "metrics": [
                {"name": "active_count", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "COUNT(loan.loan_id)"}]},
                 "datasets": ["loan"], "filter": "ghost_col = 'A'"},
            ],
        }
        issues = lint_semantics(model)
        assert any("filter 引用不在其数据集中的列 ghost_col" in i for i in issues)

    def test_metric_filter_unparseable_flagged(self):
        model = {
            "datasets": [{"name": "loan", "fields": [{"name": "status"}]}],
            "relationships": [],
            "metrics": [
                {"name": "c", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "COUNT(loan.loan_id)"}]},
                 "datasets": ["loan"], "filter": "status = = "},
            ],
        }
        issues = lint_semantics(model)
        assert any("filter 无法解析" in i for i in issues)

    def test_agg_time_dimension_not_temporal_flagged(self):
        model = {
            "datasets": [{"name": "loan", "fields": [
                {"name": "loan_id"}, {"name": "status", "semantic_role": "dimension"},
            ]}],
            "relationships": [],
            "metrics": [
                {"name": "c", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "COUNT(loan.loan_id)"}]},
                 "datasets": ["loan"], "agg_time_dimension": "loan.status"},
            ],
        }
        issues = lint_semantics(model)
        assert any("agg_time_dimension loan.status 不是时间字段" in i for i in issues)

    def test_agg_time_dimension_undeclared_flagged(self):
        model = {
            "datasets": [{"name": "loan", "fields": [{"name": "loan_id"}]}],
            "relationships": [],
            "metrics": [
                {"name": "c", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "COUNT(loan.loan_id)"}]},
                 "datasets": ["loan"], "agg_time_dimension": "loan.nope"},
            ],
        }
        issues = lint_semantics(model)
        assert any("agg_time_dimension 引用不存在的列 loan.nope" in i for i in issues)

    def test_many_to_many_flagged(self):
        model = {
            "datasets": [{"name": "a"}, {"name": "b"}],
            "relationships": [{"name": "a_to_b", "from": "a", "to": "b",
                               "cardinality": "M:N"}],
            "metrics": [],
        }
        issues = lint_semantics(model)
        assert any("M:N" in i and "fan-out" in i for i in issues)

    def test_path_ambiguity_flagged(self):
        """菱形关系图(loan-account, loan-client, client-account)→ 二义警告。"""
        model = {
            "datasets": [{"name": "loan"}, {"name": "account"}, {"name": "client"}],
            "relationships": [
                {"name": "loan_to_account", "from": "loan", "to": "account",
                 "cardinality": "1:N"},
                {"name": "loan_to_client", "from": "loan", "to": "client",
                 "cardinality": "1:N"},
                {"name": "client_to_account", "from": "client", "to": "account",
                 "cardinality": "1:N"},
            ],
            "metrics": [],
        }
        issues = lint_semantics(model)
        assert any("存在多条简单路径" in i for i in issues)

    def test_non_additive_referenced_flagged(self):
        model = {
            "datasets": [{"name": "loan", "fields": [{"name": "loan_id"}]}],
            "relationships": [],
            "metrics": [
                {"name": "distinct_loans", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "COUNT(DISTINCT loan.loan_id)"}]},
                 "non_additive": True},
                {"name": "sum_of_counts", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "SUM(distinct_loans)"}]}},
            ],
        }
        issues = lint_semantics(model)
        assert any("non_additive" in i and "sum_of_counts" in i for i in issues)

    def test_clean_model_no_issues(self):
        clean = {
            "name": "fin",
            "datasets": [{"name": "loan", "fields": [
                {"name": "amount", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "amount"}]},
                 "ai_context": {"synonyms": ["loan value"]}},
                {"name": "status", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "status"}]}},
                {"name": "date", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "date"}]}, "datatype": "Date"},
            ]}],
            "relationships": [{"name": "r", "from": "loan", "to": "loan",
                               "cardinality": "1:N"}],
            "metrics": [
                {"name": "active_count", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "COUNT(loan.loan_id)"}]},
                 "datasets": ["loan"], "filter": "status = 'A'",
                 "agg_time_dimension": "loan.date"},
            ],
        }
        assert lint_semantics(clean) == []
