"""KB lint tests — 静态检查 trove KB 的已知劣化模式.

覆盖 kb init 确定性生成暴露过的缺陷:
  - 术语 mapping 引用不存在的列
  - 对 ID/账号类列求 SUM/AVG(如 SUM(account_to))
  - 示例 SQL 无法解析或引用不存在的表
  - 列描述留空(如 district A 列 description: '')
  - lessons pattern 过长/note 为空
  - 纯中文示例对英文问题检索不可达
  - 字段级脱敏声明非法(值域越界 / hash 无 salt 引用)
"""

from pathlib import Path

import pytest
import yaml

from trove.services.kb.lint import (
    lint_examples,
    lint_lessons,
    lint_semantics,
    lint_semantics_document,
    lint_terms,
    lint_tables,
    parse_enum_values,
)
from trove.services.semantic_layer.models import MAX_FIELD_VALUES

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: 哨兵:区分「字段没有 mask 键」(存量,A11)与「mask 的值是 None」(裸 null)。
_MISSING = object()

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

    def test_metric_filter_enum_baked_flagged(self):
        """值不得写死进 metric:对已声明 enum 字段做等值过滤 → 建模异味。"""
        model = {
            "datasets": [{"name": "client", "fields": [
                {"name": "client_id"},
                {"name": "gender", "semantic_role": "enum",
                 "enum_display": {"F": "female", "M": "male"}},
            ]}],
            "relationships": [],
            "metrics": [
                {"name": "number_of_female_clients", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "COUNT(client.client_id)"}]},
                 "datasets": ["client"], "filter": "gender = 'F'"},
            ],
        }
        issues = lint_semantics(model)
        assert any("把枚举值过滤写死进 metric" in i and "gender" in i for i in issues)

    def test_metric_filter_enum_via_enum_display_flagged(self):
        """enum_display 存在(未显式 semantic_role)也算声明 enum 字段。"""
        model = {
            "datasets": [{"name": "loan", "fields": [
                {"name": "loan_id"},
                {"name": "status", "enum_display": {"A": "active", "B": "closed"}},
            ]}],
            "relationships": [],
            "metrics": [
                {"name": "active_loan_count", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "COUNT(loan.loan_id)"}]},
                 "datasets": ["loan"], "filter": "status = 'A'"},
            ],
        }
        issues = lint_semantics(model)
        assert any("把枚举值过滤写死进 metric" in i and "loan.status" in i for i in issues)

    def test_metric_filter_non_enum_not_flagged(self):
        """非枚举字段(普通 dimension)等值过滤不误报。"""
        model = {
            "datasets": [{"name": "loan", "fields": [
                {"name": "loan_id"}, {"name": "status", "semantic_role": "dimension"},
            ]}],
            "relationships": [],
            "metrics": [
                {"name": "active_loan_count", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "COUNT(loan.loan_id)"}]},
                 "datasets": ["loan"], "filter": "status = 'A'"},
            ],
        }
        issues = lint_semantics(model)
        assert not any("把枚举值过滤写死进 metric" in i for i in issues)

    def test_unique_keys_undeclared_column_flagged(self):
        model = {
            "datasets": [{"name": "client", "fields": [{"name": "client_id"}],
                          "unique_keys": [["client_id"], ["email"]]}],
            "relationships": [],
            "metrics": [],
        }
        issues = lint_semantics(model)
        assert any("unique_keys 引用未声明的列 email" in i for i in issues)

    def test_row_filter_undeclared_column_flagged(self):
        model = {
            "datasets": [{"name": "loan",
                          "fields": [{"name": "loan_id"}, {"name": "status"}],
                          "row_filter": "ghost_col = 'A'"}],
            "relationships": [],
            "metrics": [],
        }
        issues = lint_semantics(model)
        assert any("row_filter 引用未声明的列 ghost_col" in i for i in issues)

    def test_row_filter_other_table_flagged(self):
        model = {
            "datasets": [
                {"name": "loan", "fields": [{"name": "loan_id"}],
                 "row_filter": "account.region = 'EU'"},
                {"name": "account", "fields": [{"name": "region"}]},
            ],
            "relationships": [],
            "metrics": [],
        }
        issues = lint_semantics(model)
        assert any("row_filter 引用了其他表" in i for i in issues)

    def test_row_filter_unparseable_flagged(self):
        model = {
            "datasets": [{"name": "loan", "fields": [{"name": "status"}],
                          "row_filter": "status = = "}],
            "relationships": [],
            "metrics": [],
        }
        issues = lint_semantics(model)
        assert any("row_filter 无法解析" in i for i in issues)

    def test_row_filter_clean_passes(self):
        model = {
            "datasets": [{"name": "loan",
                          "fields": [{"name": "loan_id"}, {"name": "status"}],
                          "row_filter": "loan.status = 'A'"}],
            "relationships": [],
            "metrics": [],
        }
        assert lint_semantics(model) == []

    def test_metric_datatype_invalid_flagged(self):
        model = {
            "datasets": [],
            "relationships": [],
            "metrics": [
                {"name": "c", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "COUNT(loan.loan_id)"}]},
                 "datatype": "BOGUS"},
            ],
        }
        issues = lint_semantics(model)
        assert any("datatype 非法: BOGUS" in i for i in issues)

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

    def test_shared_dimension_star_not_flagged(self):
        """星型共享维度(account/client 各自 FK district,经纯维度叶 district
        二次进入)→ 编译器视为虚假路由,建模期不再误报二义(BIRD 模式)。"""
        model = {
            "datasets": [{"name": "account"}, {"name": "client"},
                         {"name": "disp"}, {"name": "district"},
                         {"name": "loan"}],
            "relationships": [
                {"name": "account_to_district", "from": "account", "to": "district",
                 "cardinality": "1:N"},
                {"name": "client_to_district", "from": "client", "to": "district",
                 "cardinality": "1:N"},
                {"name": "disp_to_account", "from": "disp", "to": "account",
                 "cardinality": "1:N"},
                {"name": "disp_to_client", "from": "disp", "to": "client",
                 "cardinality": "1:N"},
                {"name": "loan_to_account", "from": "loan", "to": "account",
                 "cardinality": "1:N"},
            ],
            "metrics": [],
        }
        issues = lint_semantics(model)
        assert not any("存在多条简单路径" in i for i in issues)

    def test_genuine_fact_diamond_flagged(self):
        """真菱形:loan—account—client,且 account 自身拥有 FK(事实/枢纽表)
        → loan↔client 两条经事实表的中转路径 → 二义警告。"""
        model = {
            "datasets": [{"name": "loan"}, {"name": "account"},
                         {"name": "client"}, {"name": "order"}],
            "relationships": [
                {"name": "loan_to_account", "from": "loan", "to": "account",
                 "cardinality": "1:N"},
                {"name": "loan_to_client", "from": "loan", "to": "client",
                 "cardinality": "1:N"},
                {"name": "client_to_account", "from": "client", "to": "account",
                 "cardinality": "1:N"},
                {"name": "account_to_order", "from": "account", "to": "order",
                 "cardinality": "1:N"},
            ],
            "metrics": [],
        }
        issues = lint_semantics(model)
        assert any("存在多条简单路径" in i and "loan" in i for i in issues)

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


class TestLintMask:
    """字段级脱敏声明(设计 §5.5 / §10 / §11 R6)。

    两条规则:
      1. ``mask`` 值域校验(``none | partial | hash | null``;**省略该键** =
         不脱敏,存量模型走这条,不得误报);
      2. 模型里有 ``hash`` 字段 → 必须声明**可解析的 salt 引用**
         (``masking.hash_salt_ref``)—— 无 salt 的 hash 可被彩虹表还原,
         不得降级为明文。

    salt 引用只认**模型级**声明。lint 是纯函数,三个调用点(管理端 issues /
    git pre-commit 门禁 / 写盘前门禁)按 ``lint_semantics_document`` 的约定
    判**同一份字节**;把部署配置引进来,同一份文档在不同机器上结论不同,而
    git 门禁恰恰要在没有密钥的 CI 上跑。模型级引用不是密文,写进 YAML 不泄漏。
    """

    @staticmethod
    def _model(fields: list[dict], masking: dict | None = None) -> dict:
        model: dict = {
            "name": "crm",
            "datasets": [{"name": "customers", "fields": fields}],
            "relationships": [],
            "metrics": [],
        }
        if masking is not None:
            model["masking"] = masking
        return model

    @staticmethod
    def _field(mask) -> dict:
        field = {"name": "phone", "expression": {"dialects": [
            {"dialect": "ANSI_SQL", "expression": "phone"}]}}
        if mask is not _MISSING:
            field["mask"] = mask
        return field

    @pytest.mark.parametrize("value", ["", "none", "partial", "null"])
    def test_valid_values_pass(self, value):
        assert lint_semantics(self._model([self._field(value)])) == []

    @pytest.mark.parametrize("value", ["bogus", "full", "sha256", "redact", 5])
    def test_invalid_value_flagged(self, value):
        issues = lint_semantics(self._model([self._field(value)]))

        assert any("phone" in i and "mask" in i for i in issues)

    def test_bare_yaml_null_flagged(self):
        """``mask: null``(裸写)被 PyYAML 解析成 None,作者本意多半是 ``null``
        模式 —— 解析器不猜(猜错方向就是静默不脱敏),由门禁拦下并提示加引号。"""
        issues = lint_semantics(self._model([self._field(None)]))

        assert any("mask" in i and '"null"' in i for i in issues)

    def test_undefined_mask_key_is_not_flagged(self):
        """A11:没有 mask 键的字段(全部存量模型)不得被误报。"""
        assert lint_semantics(self._model([self._field(_MISSING)])) == []

    def test_hash_without_salt_ref_flagged(self):
        issues = lint_semantics(self._model([self._field("hash")]))

        assert any("customers.phone" in i and "hash" in i and "salt" in i
                   for i in issues)

    def test_hash_with_empty_salt_ref_flagged(self):
        model = self._model(
            [self._field("hash")],
            {"default_policy": "apply", "bypass_scopes": [], "hash_salt_ref": ""})

        issues = lint_semantics(model)

        assert any("salt" in i for i in issues)

    def test_hash_with_salt_ref_passes(self):
        model = self._model(
            [self._field("hash")],
            {"default_policy": "apply", "bypass_scopes": ["pii"],
             "hash_salt_ref": "env:TROVE_MASK_SALT"})

        assert lint_semantics(model) == []

    def test_partial_needs_no_salt(self):
        """只有 hash 要 salt —— partial 是可人工核对的掩码,没有彩虹表问题。"""
        assert lint_semantics(self._model([self._field("partial")])) == []

    def test_salt_issue_is_reported_once_per_model(self):
        model = self._model([self._field("hash"), self._field("hash")])
        model["datasets"][0]["fields"][1] = {
            "name": "id_card", "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": "id_card"}]},
            "mask": "hash"}

        issues = lint_semantics(model)

        salt_issues = [i for i in issues if "salt" in i]
        assert len(salt_issues) == 1
        assert "customers.phone" in salt_issues[0]
        assert "customers.id_card" in salt_issues[0]

    def test_document_level_masking_flagged(self):
        """``masking`` 挂在模型入口下(与 time_spine 同层),文档顶层是哑的。

        设计稿 §5.5 注释写「semantics.yml 顶层」,单模型文件里读起来像文档
        顶层;解析器只认模型入口,写错层会被静默忽略 —— 而静默忽略 =
        静默不脱敏,所以在这里报出来。
        """
        doc = {
            "masking": {"default_policy": "bypass"},
            "semantic_model": [{"name": "crm", "datasets": [], "metrics": []}],
        }

        issues = lint_semantics_document(doc)

        assert any("masking" in i and "semantic_model" in i for i in issues)

    @pytest.mark.parametrize("datasource", ["demo", "financial", "mysql_fin"])
    def test_stock_semantics_document_is_clean(self, datasource):
        """A11:存量 semantics.yml 整份 lint 结果不变(新规则不得误报)。"""
        path = (_REPO_ROOT / ".trove" / "kb" / datasource / "semantics.yml")

        assert lint_semantics_document(
            yaml.safe_load(path.read_text(encoding="utf-8")), dialect="sqlite") == []



class TestLintValues:
    """字段级 ``values``(列的实际取值)形状门。

    它是**结构事实**,所以 lint 只管形状,不管语义:
      - 必须是非空 string 数组(映射/嵌套/空条目 = 坏形状);
      - ≤ MAX_FIELD_VALUES(超限说明探测落了残缺取值域 —— 残缺的"事实"
        比没有更坏,值路由会当完整词表用);
      - 时间字段不得声明(日期取值是时点,不是取值词表)。

    省略该键 = 未探测(存量模型走这条,不得误报)。
    """

    @staticmethod
    def _model(field_extra: dict | None = None, field_name: str = "A2",
               **field_kwargs) -> dict:
        field = {
            "name": field_name,
            "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": field_name}]},
            "datatype": "String",
        }
        field.update(field_extra or {})
        field.update(field_kwargs)
        return {
            "name": "fin",
            "datasets": [{"name": "district", "fields": [field]}],
            "relationships": [],
            "metrics": [],
        }

    def test_valid_values_pass(self):
        assert lint_semantics(self._model({"values": ["Sokolov", "Benesov"]})) == []

    def test_absent_key_not_flagged(self):
        """存量模型(无 values 键)不得被误报。"""
        assert lint_semantics(self._model({})) == []

    @pytest.mark.parametrize("bad", ["Sokolov", {"a": 1}, ["ok", 7], ["ok", ""], [[]]])
    def test_bad_shape_flagged(self, bad):
        issues = lint_semantics(self._model({"values": bad}))

        assert any("values" in i for i in issues)

    def test_over_limit_flagged(self):
        issues = lint_semantics(self._model(
            {"values": [f"v{i}" for i in range(MAX_FIELD_VALUES + 1)]}))

        assert any("district.A2" in i and "values" in i for i in issues)

    def test_at_limit_passes(self):
        assert lint_semantics(self._model(
            {"values": [f"v{i}" for i in range(MAX_FIELD_VALUES)]})) == []

    @pytest.mark.parametrize("extra", [
        {"semantic_role": "time"},
        {"datatype": "Date"},
        {"datatype": "DateTimeTz"},
    ])
    def test_temporal_field_flagged(self, extra):
        issues = lint_semantics(self._model({"values": ["1993-01-15"]}, **extra))

        assert any("时间字段" in i for i in issues)

    def _with_baked_filter(self, **field_kwargs) -> dict:
        """一份"metric filter 对某字段等值过滤"的模型(建模异味探针)。"""
        model = self._model(field_name="a2", **field_kwargs)
        model["metrics"] = [{
            "name": "m",
            "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": "COUNT(district.a2)"}]},
            "filter": "district.a2 = 'Sokolov'",
        }]
        return model

    def test_values_do_not_make_an_enum_field(self):
        """``values`` 是数据,不是枚举声明:它不让字段变成 enum 字段。

        枚举字段判定读的是 ``semantic_role`` / ``enum_display``;若把
        ``values`` 也认成枚举词表,下面这条 metric 会被误判成"把枚举值写死
        进 metric"—— 这正是预冻结接口 Ⅰ 要挡住的事(对照组见下一个用例)。
        """
        issues = lint_semantics(self._with_baked_filter(values=["Sokolov"]))

        assert not any("写死" in i for i in issues)

    def test_enum_display_still_flags_baked_filter(self):
        """对照组:同一份文档,字段改成真枚举(enum_display)→ 规则照常命中。
        证明上面那条不是因为规则坏了才通过的。"""
        issues = lint_semantics(self._with_baked_filter(
            enum_display={"Sokolov": "Sokolov"}, semantic_role="enum"))

        assert any("写死" in i for i in issues)
