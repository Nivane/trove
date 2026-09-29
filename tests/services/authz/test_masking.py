"""字段级脱敏单测:四种模式、列匹配、scope 判定、salt 失败方向。

对应设计文档 ``2026-09-28-agent-identity-masking-design.md`` 的
§5.5 / §5.6 / §6.3 / §8.3 / §8.4 / §10 / R5 / R6,验收项 A5 / A6 / A7 / A11。

三条被测试钉死的边界(每条都比"实现看起来对不对"更容易被后来的改动破坏):

1. **列匹配优先按投影位置,退化才按列名。** 位置信息在场时,"输出列名恰好
   叫 phone"不构成脱敏依据 —— 否则 ``SELECT email AS phone`` 会按 phone 的
   模式去改写 email 的值。
2. **函数/聚合投影永不匹配**(``COUNT(DISTINCT phone)``)。掩码一个计数是错的
   (设计 §8.3 选"结果集后置脱敏"正是为了保住聚合能力)。
3. **空 scopes 不是"不限"。** §5.6:``on_behalf_of`` 重放不可叠加 bypass ——
   若这里误用 ``scopes_allow``(空 = 不限),每一个存量 token 和每一次重放
   都会直接看到原文。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from trove.services.authz.masking import Masker, MaskingError, resolve_salt_ref
from trove.services.authz.policy import Principal
from trove.services.semantic_layer.models import (
    MaskingPolicy,
    SemanticDataset,
    SemanticField,
    SemanticModel,
)

SALT_ENV = "TROVE_TEST_MASK_SALT"
SALT = "s3cr3t-salt"
SALT_REF = f"env:{SALT_ENV}"


@pytest.fixture
def salt_env(monkeypatch):
    monkeypatch.setenv(SALT_ENV, SALT)
    return SALT


# ── 夹具构造 ────────────────────────────────────────────────────


def _field(name: str, mask: str = "", expression: str = "") -> SemanticField:
    return SemanticField(name=name, expression=expression or name, mask=mask)


def _ds(*fields: SemanticField, name: str = "customers") -> SemanticDataset:
    return SemanticDataset(name=name, source=f"public.{name}", fields=list(fields))


def _model(*datasets: SemanticDataset, masking: MaskingPolicy | None = None) -> SemanticModel:
    return SemanticModel(datasets=list(datasets), masking=masking or MaskingPolicy())


def _proj(fn: str | None, *cols: tuple[str, str]) -> list:
    """契约投影 wire(``signature_to_wire`` 的形状):``[fn, [[表, 列], ...]]``。"""
    return [fn, [[t, c] for t, c in cols]]


def _contract(*projections: list) -> dict:
    return {"skeleton_sql": "SELECT 1", "signature": {"projections": list(projections)}}


def _masker(default: str = SALT_REF) -> Masker:
    return Masker(default_salt_ref=default)


def _hash(value: str, salt: str = SALT) -> str:
    """设计 §5.5 的公式,照抄一遍(测试不依赖实现内部):
    ``sha256(salt + value)[:12]``。"""
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()[:12]


# ── A11:没声明就没行为 ──────────────────────────────────────────


class TestNoDeclarationIsInert:
    """存量语义模型无 ``mask`` → 行为不变(验收项 A11)。"""

    def test_no_declarations_returns_rows_and_empty_report(self):
        model = _model(_ds(_field("phone"), _field("city")))
        rows = [["13812348888", "sh"]]

        out, report = _masker().apply(rows, ["phone", "city"], model=model,
                                      principal=Principal(subject="1"))

        assert out == rows
        assert report == {"fields": {}, "bypass": False}

    @pytest.mark.parametrize("declared", ["", "none"])
    def test_none_and_empty_masks_are_both_inert(self, declared):
        """``""`` 与 ``none`` 同义(§6.1);两者都不算"声明了脱敏"。"""
        model = _model(_ds(_field("phone", mask=declared)))
        rows = [["13812348888"]]

        out, report = _masker().apply(rows, ["phone"], model=model,
                                      principal=Principal(subject="1"))

        assert out == [["13812348888"]]
        assert report == {"fields": {}, "bypass": False}

    def test_no_model_is_inert(self):
        """``model=None`` = 没有声明面,不是"没有依据所以放行"。"""
        out, report = _masker().apply([["x"]], ["c"], model=None,
                                      principal=Principal(subject="1"))
        assert out == [["x"]]
        assert report == {"fields": {}, "bypass": False}


# ── A5:四种模式 ─────────────────────────────────────────────────


class TestFourModes:
    def test_none_keeps_value(self):
        model = _model(_ds(_field("phone", mask="none")))
        out, report = _masker().apply([["13812348888"]], ["phone"], model=model,
                                      principal=Principal(subject="1"))
        assert out == [["13812348888"]]
        assert report["fields"] == {}

    def test_partial_keeps_head_and_tail(self, salt_env):
        """手机号形状就是设计 §5.5 举的那一个:``138****8888``。"""
        model = _model(_ds(_field("phone", mask="partial")))
        out, report = _masker().apply([["13812348888"]], ["phone"], model=model,
                                      principal=Principal(subject="1"))
        assert out == [["138****8888"]]
        assert report["fields"] == {"phone": "partial"}

    def test_hash_is_salted_sha256_prefix(self, salt_env):
        model = _model(_ds(_field("id_card", mask="hash")))
        out, _ = _masker().apply([["110101199003078515"]], ["id_card"], model=model,
                                 principal=Principal(subject="1"))
        assert out == [[_hash("110101199003078515")]]
        assert len(out[0][0]) == 12

    def test_null_always_none(self):
        model = _model(_ds(_field("id_card", mask="null")))
        out, report = _masker().apply([["110101199003078515"]], ["id_card"], model=model,
                                      principal=Principal(subject="1"))
        assert out == [[None]]
        assert report["fields"] == {"id_card": "null"}

    def test_all_four_in_one_pass(self, salt_env):
        model = _model(_ds(
            _field("city", mask="none"),
            _field("phone", mask="partial"),
            _field("id_card", mask="hash"),
            _field("salary", mask="null"),
        ))
        rows = [["sh", "13812348888", "110101199003078515", 9000]]

        out, report = _masker().apply(
            rows, ["city", "phone", "id_card", "salary"], model=model,
            principal=Principal(subject="1"),
        )

        assert out == [["sh", "138****8888", _hash("110101199003078515"), None]]
        assert report["fields"] == {"phone": "partial", "id_card": "hash", "salary": "null"}

    def test_unknown_mask_value_is_refused_not_ignored(self):
        """非法声明在 lint 阶段拦(§10),但运行时**不得**当成"没声明"放行 ——
        那正是"以为脱敏了其实没有"。"""
        model = _model(_ds(_field("phone", mask="partital")))
        with pytest.raises(MaskingError):
            _masker().apply([["13812348888"]], ["phone"], model=model,
                            principal=Principal(subject="1"))


class TestPartialShape:
    """短值规则要确定:留首尾在极短值上等于不遮(``ab`` → ``ab``)。"""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("13812348888", "138" + "*" * 4 + "8888"),         # 11:设计里的例子
            ("110101199003078515", "110" + "*" * 11 + "8515"),  # 18:身份证
            ("alice@example.com", "ali" + "*" * 10 + ".com"),
            ("1380013800", "1" + "*" * 8 + "0"),               # 10:降一档
            ("123", "1*3"),
            ("ab", "**"),                                      # 留首尾 = 不遮
            ("a", "*"),
            ("", ""),                                          # 空串不泄任何东西
        ],
    )
    def test_deterministic_shapes(self, value, expected):
        model = _model(_ds(_field("v", mask="partial")))
        out, _ = _masker().apply([[value]], ["v"], model=model,
                                 principal=Principal(subject="1"))
        assert out == [[expected]]


# ── 值处理:None 与类型 ─────────────────────────────────────────


class TestValueHandling:
    @pytest.mark.parametrize("mode", ["none", "", "partial", "hash", "null"])
    def test_none_stays_none_in_every_mode(self, mode, salt_env):
        """``None`` 是"没有值",不是"值里的 PII" —— 四种模式下都不得变成
        ``"***"`` / 哈希 / 空串,否则下游读不出"这行没有值"。"""
        model = _model(_ds(_field("phone", mask=mode)))
        out, _ = _masker().apply([[None]], ["phone"], model=model,
                                 principal=Principal(subject="1"))
        assert out == [[None]]

    def test_numbers_are_stringified_before_masking(self, salt_env):
        """掩码列是展示值:类型变化是**有意**的(见 ``Masker`` docstring)。"""
        model = _model(_ds(_field("phone", mask="partial"), _field("pid", mask="hash")))
        out, _ = _masker().apply([[13812348888, 42]], ["phone", "pid"], model=model,
                                 principal=Principal(subject="1"))
        assert out == [["138****8888", _hash("42")]]
        assert isinstance(out[0][0], str)

    def test_int_and_str_of_the_same_value_hash_alike(self, salt_env):
        """同一列在不同后端可能回来 int 或 str;哈希要一致,否则分组统计被拆开。"""
        model = _model(_ds(_field("pid", mask="hash")))
        out, _ = _masker().apply([[42], ["42"]], ["pid"], model=model,
                                 principal=Principal(subject="1"))
        assert out[0] == out[1]


# ── 列匹配:位置优先 ─────────────────────────────────────────────


class TestPositionalMatching:
    def test_contract_positions_decide_even_when_names_differ(self):
        """带别名的裸列:输出名是 ``p``,位置告诉我们是 ``customers.phone``。"""
        model = _model(_ds(_field("phone", mask="partial")))
        out, report = _masker().apply(
            [["13812348888"]], ["p"], model=model, principal=Principal(subject="1"),
            contract=_contract(_proj(None, ("customers", "phone"))),
        )
        assert out == [["138****8888"]]
        assert report["fields"] == {"phone": "partial"}

    def test_contract_beats_output_column_names(self):
        """位置与列名冲突时位置赢:``SELECT email AS phone`` 必须按 email 的模式走。"""
        model = _model(_ds(_field("phone", mask="null"), _field("email", mask="partial")))
        out, report = _masker().apply(
            [["a@b.com"]], ["phone"], model=model, principal=Principal(subject="1"),
            contract=_contract(_proj(None, ("customers", "email"))),
        )
        assert out == [["a*****m"]]          # "a@b.com" 的 partial 形状
        assert report["fields"] == {"email": "partial"}

    def test_contract_beats_sql(self, salt_env):
        """两份位置信息都在场时契约优先(它是编译期抽的权威结构)。"""
        model = _model(_ds(_field("phone", mask="null"), _field("email", mask="hash")))
        out, report = _masker().apply(
            [["a@b.com"]], ["x"], model=model, principal=Principal(subject="1"),
            sql="SELECT phone FROM customers",
            contract=_contract(_proj(None, ("customers", "email"))),
        )
        assert out == [[_hash("a@b.com")]]
        assert report["fields"] == {"email": "hash"}

    def test_sql_positions_decide_when_no_contract(self, salt_env):
        model = _model(_ds(_field("phone", mask="partial")))
        out, report = _masker().apply(
            [["13812348888"]], ["p"], model=model, principal=Principal(subject="1"),
            sql="SELECT c.phone AS p FROM customers AS c",
        )
        assert out == [["138****8888"]]
        assert report["fields"] == {"phone": "partial"}

    def test_position_info_suppresses_the_name_fallback(self):
        """**位置信息在场时"列名恰好叫 phone"不构成依据。**

        反例才是重点:``SELECT email AS phone`` 的名字退化会拿 phone 的模式去
        改写 email —— 这里位置说第 0 位是 email(未声明脱敏),于是原样返回。
        """
        model = _model(_ds(_field("phone", mask="null"), _field("email")))
        out, report = _masker().apply(
            [["a@b.com"]], ["phone"], model=model, principal=Principal(subject="1"),
            contract=_contract(_proj(None, ("customers", "email"))),
        )
        assert out == [["a@b.com"]]
        assert report == {"fields": {}, "bypass": False}

    def test_aggregate_position_is_never_masked(self):
        """``COUNT(DISTINCT phone)`` 不是 phone —— 掩码一个计数是错的(§8.3)。"""
        model = _model(_ds(_field("phone", mask="partial")))
        out, report = _masker().apply(
            [[7, "13812348888"]], ["n", "phone"], model=model,
            principal=Principal(subject="1"),
            contract=_contract(
                _proj("count", ("customers", "phone")),
                _proj(None, ("customers", "phone")),
            ),
        )
        assert out == [[7, "138****8888"]]
        assert report["fields"] == {"phone": "partial"}

    def test_aggregate_aliased_as_the_field_name_is_still_not_masked(self):
        """**边界测试的硬版本**:别名把 ``COUNT(...)`` 叫成 ``phone``。

        输出列名与字段名逐字相同 —— 位置信息说这个位置"不是一个列",于是列名
        兜底不得接管它。少了这条边界,一个计数会被掩成 ``None``(§8.3 选后置
        脱敏就是为了保住这类聚合)。
        """
        model = _model(_ds(_field("phone", mask="null")))
        out, report = _masker().apply(
            [[7]], ["phone"], model=model, principal=Principal(subject="1"),
            contract=_contract(_proj("count", ("customers", "phone"))),
        )
        assert out == [[7]]
        assert report == {"fields": {}, "bypass": False}

    def test_scalar_function_aliased_as_the_field_name_is_still_not_masked(self):
        """sqlglot 路径的同一条边界:``LOWER(email) AS email``。"""
        model = _model(_ds(_field("email", mask="null")))
        out, report = _masker().apply(
            [["a@b.com"]], ["email"], model=model, principal=Principal(subject="1"),
            sql="SELECT LOWER(email) AS email FROM customers",
        )
        assert out == [["a@b.com"]]
        assert report == {"fields": {}, "bypass": False}

    def test_sql_function_projection_is_never_masked(self):
        """sqlglot 路径更严:标量函数也看得见,``LOWER(email)`` 不匹配。"""
        model = _model(_ds(_field("email", mask="null")))
        out, report = _masker().apply(
            [["a@b.com"]], ["lower_email"], model=model, principal=Principal(subject="1"),
            sql="SELECT LOWER(email) AS lower_email FROM customers",
        )
        assert out == [["a@b.com"]]
        assert report["fields"] == {}

    def test_sql_bare_column_matches(self):
        model = _model(_ds(_field("email", mask="null")))
        out, _ = _masker().apply(
            [["a@b.com"]], ["email"], model=model, principal=Principal(subject="1"),
            sql="SELECT email FROM customers",
        )
        assert out == [[None]]

    def test_multi_column_contract_projection_is_not_masked(self):
        """契约里多列表达式(``first_name || last_name``)不是一个列身份 ——
        不能只取第一列去匹配(那会把拼接结果当成 first_name 掩掉)。"""
        model = _model(_ds(_field("first_name", mask="null")))
        out, report = _masker().apply(
            [["ab"]], ["full"], model=model, principal=Principal(subject="1"),
            contract=_contract(
                _proj(None, ("customers", "first_name"), ("customers", "last_name"))
            ),
        )
        assert out == [["ab"]]
        assert report == {"fields": {}, "bypass": False}

    def test_expression_with_multiple_columns_is_not_masked(self):
        """``a || b`` 不是一个列身份 —— 拿谁的模式都不对。"""
        model = _model(_ds(_field("first_name", mask="partial")))
        out, report = _masker().apply(
            [["ab"]], ["full"], model=model, principal=Principal(subject="1"),
            sql="SELECT first_name || last_name AS full FROM customers",
        )
        assert out == [["ab"]]
        assert report["fields"] == {}


class TestNameNormalization:
    def test_qualification_quotes_and_case_are_normalized(self):
        """表限定、引号/反引号/方括号、大小写、空白都不该让匹配失效。"""
        model = _model(_ds(_field("Phone", mask="partial", expression="public.customers.Phone")))
        out, _ = _masker().apply(
            [["13812348888"]], ["  `Phone` "], model=model, principal=Principal(subject="1"),
        )
        assert out == [["138****8888"]]

    def test_case_folding_is_applied_to_both_sides(self):
        """声明与输出两侧的大小写差异都要能被吸收(只归一化一侧等于没归一化)。"""
        model = _model(_ds(_field("phone", mask="null")))
        out, _ = _masker().apply([["13812348888"]], ["PHONE"], model=model,
                                 principal=Principal(subject="1"))
        assert out == [[None]]

    def test_field_expression_tail_is_a_match_key(self):
        """字段名与物理列名不一致时,``expression`` 末段是桥。"""
        model = _model(_ds(_field("mobile", mask="partial", expression="customers.phone")))
        out, _ = _masker().apply([["13812348888"]], ["phone"], model=model,
                                 principal=Principal(subject="1"))
        assert out == [["138****8888"]]

    def test_quoted_catalog_names_normalize(self):
        model = _model(_ds(_field("phone", mask="null")))
        out, _ = _masker().apply(
            [["13812348888"]], ["phone"], model=model, principal=Principal(subject="1"),
            contract=_contract(_proj(None, ("[CUSTOMERS]", '"Phone"'))),
        )
        assert out == [[None]]


class TestStrictestWins:
    """同一输出位置命中多个声明 → 取最严格(partial < hash < null)。"""

    def test_two_datasets_same_field_name_different_modes(self, salt_env):
        model = _model(
            _ds(_field("phone", mask="partial"), name="crm"),
            _ds(_field("phone", mask="null"), name="dw"),
        )
        out, report = _masker().apply(
            [["13812348888"]], ["phone"], model=model, principal=Principal(subject="1"),
            contract=_contract(_proj(None, ("", "phone"))),   # 未限定 → 两个都命中
        )
        assert out == [[None]]
        assert report["fields"] == {"phone": "null"}

    @pytest.mark.parametrize(
        ("table", "expected"),
        [("crm", "138****8888"), ("dw", None)],
    )
    def test_table_qualifier_narrows_candidates(self, table, expected, salt_env):
        """有表限定且能对上某个数据集时,**不要**把同名的最严模式糊上去。"""
        model = _model(
            _ds(_field("phone", mask="partial"), name="crm"),
            _ds(_field("phone", mask="null"), name="dw"),
        )
        out, report = _masker().apply(
            [["13812348888"]], ["phone"], model=model, principal=Principal(subject="1"),
            contract=_contract(_proj(None, (table, "phone"))),
        )
        assert out == [[expected]]
        assert report["fields"]["phone"] == ("partial" if table == "crm" else "null")

    def test_unresolvable_table_alias_falls_back_to_all_name_matches(self, salt_env):
        """``c`` 是别名不是数据集名 → 不能因为"对不上"就一个都不匹配(那会漏脱敏),
        退回按列名全部命中、取最严。"""
        model = _model(
            _ds(_field("phone", mask="partial"), name="crm"),
            _ds(_field("phone", mask="hash"), name="dw"),
        )
        out, report = _masker().apply(
            [["13812348888"]], ["phone"], model=model, principal=Principal(subject="1"),
            contract=_contract(_proj(None, ("c", "phone"))),
        )
        assert out == [[_hash("13812348888")]]
        assert report["fields"] == {"phone": "hash"}

    def test_same_position_matches_two_fields_of_one_dataset(self, salt_env):
        """字段名与另一个字段的 expression 末段撞上 → 取严。"""
        model = _model(_ds(
            _field("phone", mask="partial"),
            _field("mobile", mask="hash", expression="customers.phone"),
        ))
        out, report = _masker().apply([["13812348888"]], ["phone"], model=model,
                                      principal=Principal(subject="1"))
        assert out == [[_hash("13812348888")]]
        assert report["fields"] == {"phone": "hash", "mobile": "hash"}


# ── 位置信息不可用时的退化 ───────────────────────────────────────


class TestDegradation:
    """位置信息是**增强不是前提**(见 ``Masker`` docstring)。"""

    def test_unparseable_sql_degrades_to_names_without_raising(self):
        model = _model(_ds(_field("phone", mask="partial")))
        out, report = _masker().apply(
            [["13812348888"]], ["phone"], model=model, principal=Principal(subject="1"),
            sql="SELECT FROM WHERE ((( ",
        )
        assert out == [["138****8888"]]
        assert report["fields"] == {"phone": "partial"}

    def test_misaligned_contract_degrades_to_names(self, salt_env):
        """投影数与列数不一致(``SELECT *`` / 契约来自另一条 SQL)→ 位置不可用。"""
        model = _model(_ds(_field("phone", mask="partial")))
        out, report = _masker().apply(
            [["13812348888"]], ["phone"], model=model, principal=Principal(subject="1"),
            contract=_contract(
                _proj(None, ("customers", "email")),
                _proj(None, ("customers", "city")),
            ),
        )
        assert out == [["138****8888"]]
        assert report["fields"] == {"phone": "partial"}

    def test_contract_without_signature_falls_back_to_sql(self):
        """签名抽取失败(``signature=None``,契约里明写的合法取值)→ 用 SQL 的位置。"""
        model = _model(_ds(_field("phone", mask="partial")))
        out, report = _masker().apply(
            [["13812348888"]], ["phone"], model=model, principal=Principal(subject="1"),
            sql="SELECT phone FROM customers",
            contract={"skeleton_sql": "SELECT 1", "signature": None},
        )
        assert out == [["138****8888"]]
        assert report["fields"] == {"phone": "partial"}

    def test_star_projection_degrades_to_names(self):
        """``SELECT *`` 不是位置表:表达式个数与输出列数只是**偶然**相等。

        单列表上 1 个 ``*`` 对 1 个 ``phone`` 看着对齐,若信了它,那个叫 phone
        的列反而不会脱敏 —— 而它就明明白白在结果里。宁可退回按列名匹配。
        """
        model = _model(_ds(_field("phone", mask="partial")))
        out, report = _masker().apply(
            [["13812348888"]], ["phone"], model=model, principal=Principal(subject="1"),
            sql="SELECT * FROM customers",
        )
        assert out == [["138****8888"]]
        assert report["fields"] == {"phone": "partial"}

    def test_qualified_star_degrades_to_names(self):
        model = _model(_ds(_field("phone", mask="partial")))
        out, _ = _masker().apply(
            [["13812348888"]], ["phone"], model=model, principal=Principal(subject="1"),
            sql="SELECT c.* FROM customers AS c",
        )
        assert out == [["138****8888"]]

    def test_garbage_contract_degrades_to_names(self):
        model = _model(_ds(_field("phone", mask="null")))
        out, _ = _masker().apply(
            [["13812348888"]], ["phone"], model=model, principal=Principal(subject="1"),
            contract={"signature": {"projections": "nonsense"}},
        )
        assert out == [[None]]


# ── A7 / §5.6 / §8.4:scope 判定 ─────────────────────────────────


class TestScopeJudgement:
    def test_without_pii_scope_values_are_masked(self, salt_env):
        model = _model(_ds(_field("id_card", mask="hash")))
        out, report = _masker().apply(
            [["110101199003078515"]], ["id_card"], model=model,
            principal=Principal(subject="7", scopes=frozenset({"query"})),
        )
        assert out == [[_hash("110101199003078515")]]
        assert report["bypass"] is False

    def test_pii_scope_sees_plaintext(self, salt_env):
        model = _model(
            _ds(_field("id_card", mask="hash")),
            masking=MaskingPolicy(bypass_scopes=["pii"]),
        )
        out, report = _masker().apply(
            [["110101199003078515"]], ["id_card"], model=model,
            principal=Principal(subject="7", scopes=frozenset({"pii"})),
        )
        assert out == [["110101199003078515"]]
        assert report == {"fields": {}, "bypass": True}

    def test_bypass_scopes_are_declared_per_model_not_built_in(self, salt_env):
        """``pii`` 不是内建口令 —— 模型没声明 ``bypass_scopes`` 时谁都看原文不了。"""
        model = _model(_ds(_field("id_card", mask="hash")))
        out, report = _masker().apply(
            [["110101199003078515"]], ["id_card"], model=model,
            principal=Principal(subject="7", scopes=frozenset({"pii"})),
        )
        assert out == [[_hash("110101199003078515")]]
        assert report["bypass"] is False

    def test_admin_without_pii_is_still_masked(self, salt_env):
        """§8.4:admin 是运维角色,不是数据授权角色 —— 不自动 bypass。"""
        model = _model(_ds(_field("id_card", mask="hash")))
        out, report = _masker().apply(
            [["110101199003078515"]], ["id_card"], model=model,
            principal=Principal(subject="1", role="admin"),
        )
        assert out == [[_hash("110101199003078515")]]
        assert report["bypass"] is False

    def test_legacy_principal_with_empty_scopes_is_masked(self, salt_env):
        """§5.6:空 scopes 必须意味着"没有 pii"。

        走 ``scopes_allow`` 的话空集合读作"不限" → 每一个存量 token 直接看
        原文。这条测试就是那条规则的方向守卫。
        """
        model = _model(_ds(_field("id_card", mask="hash")))
        out, report = _masker().apply(
            [["110101199003078515"]], ["id_card"], model=model,
            principal=Principal(subject="L", role="admin", scopes=frozenset()),
        )
        assert report["bypass"] is False
        assert out == [[_hash("110101199003078515")]]

    def test_replayed_principal_is_judged_by_its_own_scopes(self, salt_env):
        """§5.6:重放按**目标用户**的权限走 —— 目标自己持 pii 才算有权。"""
        model = _model(_ds(_field("id_card", mask="hash")))
        replayed = Principal(subject="42", scopes=frozenset(), on_behalf_of="7")
        out, report = _masker().apply(
            [["110101199003078515"]], ["id_card"], model=model, principal=replayed,
        )
        assert report["bypass"] is False
        assert out == [[_hash("110101199003078515")]]

    def test_default_policy_bypass_turns_masking_off_for_everyone(self, salt_env):
        model = _model(
            _ds(_field("id_card", mask="hash")),
            masking=MaskingPolicy(default_policy="bypass"),
        )
        out, report = _masker().apply(
            [["110101199003078515"]], ["id_card"], model=model,
            principal=Principal(subject="7", scopes=frozenset()),
        )
        assert out == [["110101199003078515"]]
        assert report == {"fields": {}, "bypass": True}

    def test_no_principal_masks(self, salt_env):
        """没有主体时**没有**可依据的 pii 授权 —— 安全方向是多脱敏,不是放行。"""
        model = _model(_ds(_field("id_card", mask="hash")))
        out, report = _masker().apply([["110101199003078515"]], ["id_card"], model=model,
                                      principal=None)
        assert report["bypass"] is False
        assert out == [[_hash("110101199003078515")]]

    def test_bypass_does_not_require_a_salt(self):
        """bypass 路径不得因为"要算哈希"而先解析 salt 再失败 —— 一个都不算。"""
        model = _model(
            _ds(_field("id_card", mask="hash")),
            masking=MaskingPolicy(bypass_scopes=["pii"]),
        )
        out, report = _masker(default="").apply(
            [["110101199003078515"]], ["id_card"], model=model,
            principal=Principal(subject="7", scopes=frozenset({"pii"})),
        )
        assert out == [["110101199003078515"]]
        assert report == {"fields": {}, "bypass": True}

    def test_bypass_returns_copies_not_the_caller_lists(self):
        model = _model(
            _ds(_field("id_card", mask="null")),
            masking=MaskingPolicy(bypass_scopes=["pii"]),
        )
        rows = [["110101199003078515"]]
        out, _ = _masker().apply(rows, ["id_card"], model=model,
                                 principal=Principal(subject="7", scopes=frozenset({"pii"})))
        assert out == rows
        assert out is not rows and out[0] is not rows[0]


# ── salt:必须解析得出,否则拒绝(§10 / R6)──────────────────────


class TestSalt:
    def test_resolve_env_ref(self, salt_env):
        assert resolve_salt_ref(SALT_REF) == SALT

    def test_missing_env_var_is_refused(self, monkeypatch):
        monkeypatch.delenv(SALT_ENV, raising=False)
        model = _model(_ds(_field("id_card", mask="hash")))
        with pytest.raises(MaskingError):
            _masker().apply([["1"]], ["id_card"], model=model,
                            principal=Principal(subject="1"))

    def test_empty_ref_is_refused(self):
        """§10:降级为明文即泄漏 —— 解析不出就不执行。"""
        model = _model(_ds(_field("id_card", mask="hash")))
        with pytest.raises(MaskingError):
            _masker(default="").apply([["1"]], ["id_card"], model=model,
                                      principal=Principal(subject="1"))

    def test_unknown_ref_shape_is_refused(self, salt_env):
        model = _model(_ds(_field("id_card", mask="hash")))
        with pytest.raises(MaskingError):
            _masker(default="vault:secret/mask").apply(
                [["1"]], ["id_card"], model=model, principal=Principal(subject="1"))

    def test_salt_is_not_required_without_hash_fields(self):
        """只有 partial/null 时不该因为缺 salt 拒绝:那是把安全改进做成事故。"""
        model = _model(_ds(_field("phone", mask="partial"), _field("id_card", mask="null")))
        out, _ = _masker(default="").apply(
            [["13812348888", "x"]], ["phone", "id_card"], model=model,
            principal=Principal(subject="1"),
        )
        assert out == [["138****8888", None]]

    def test_model_ref_wins_over_deployment_default(self, monkeypatch):
        monkeypatch.setenv(SALT_ENV, "deployment")
        monkeypatch.setenv("TROVE_MODEL_SALT", "model-level")
        model = _model(
            _ds(_field("id_card", mask="hash")),
            masking=MaskingPolicy(hash_salt_ref="env:TROVE_MODEL_SALT"),
        )
        out, _ = _masker().apply([["1"]], ["id_card"], model=model,
                                 principal=Principal(subject="1"))
        assert out == [[_hash("1", salt="model-level")]]

    def test_deployment_default_used_when_model_ref_is_empty(self, salt_env):
        model = _model(_ds(_field("id_card", mask="hash")))
        out, _ = _masker().apply([["1"]], ["id_card"], model=model,
                                 principal=Principal(subject="1"))
        assert out == [[_hash("1")]]

    def test_different_salt_yields_different_hash(self, monkeypatch):
        """R6:salt 不参与则彩虹表可还原手机号。"""
        model = _model(_ds(_field("phone", mask="hash")))
        monkeypatch.setenv(SALT_ENV, "one")
        first, _ = _masker().apply([["13812348888"]], ["phone"], model=model,
                                   principal=Principal(subject="1"))
        monkeypatch.setenv(SALT_ENV, "two")
        second, _ = _masker().apply([["13812348888"]], ["phone"], model=model,
                                    principal=Principal(subject="1"))
        assert first != second

    def test_resolution_happens_once_per_call(self, monkeypatch):
        """注入点:部署可以整体替换成 KMS 取 salt。整批只解析一次。"""
        calls: list[str] = []

        def fake(ref: str) -> str:
            calls.append(ref)
            return SALT

        monkeypatch.setattr("trove.services.authz.masking.resolve_salt_ref", fake)
        model = _model(_ds(_field("pid", mask="hash")))
        out, _ = _masker().apply([[1], [2], [3]], ["pid"], model=model,
                                 principal=Principal(subject="1"))
        assert calls == [SALT_REF]
        assert out[0] == [_hash("1")]


# ── A6:同值同哈希(可分组统计)──────────────────────────────────


class TestA6Grouping:
    def test_same_value_same_hash_across_rows(self, salt_env):
        model = _model(_ds(_field("id_card", mask="hash")))
        out, _ = _masker().apply(
            [["110101199003078515"], ["110101199003078515"], ["22020219900307852X"]],
            ["id_card"], model=model, principal=Principal(subject="1"),
        )
        assert out[0] == out[1]
        assert out[0] != out[2]

    def test_distinct_count_semantics_survive(self, salt_env):
        """验收项 A6 的实质:``COUNT(DISTINCT hash(id_card))`` 的结果不变。

        这里在结果集上做一次分组计数 —— 脱敏若不是同值同哈希,组数会变大。
        """
        model = _model(_ds(_field("id_card", mask="hash")))
        rows = [["a"], ["a"], ["b"]]
        out, _ = _masker().apply(rows, ["id_card"], model=model,
                                 principal=Principal(subject="1"))
        assert len({r[0] for r in out}) == len({r[0] for r in rows}) == 2


# ── §6.3 审计面 ─────────────────────────────────────────────────


class TestFieldsReport:
    def test_reports_declared_field_name_and_mode(self):
        model = _model(_ds(_field("phone", mask="partial", expression="customers.phone")))
        _, report = _masker().apply([["13812348888"]], ["phone"], model=model,
                                    principal=Principal(subject="1"))
        assert report == {"fields": {"phone": "partial"}, "bypass": False}

    def test_only_applied_fields_are_reported(self):
        """声明的字段没出现在结果里 → 没"对它做了什么",不该进审计。"""
        model = _model(_ds(_field("phone", mask="partial"), _field("id_card", mask="hash")))
        _, report = _masker().apply([["13812348888"]], ["phone"], model=model,
                                    principal=Principal(subject="1"))
        assert report["fields"] == {"phone": "partial"}

    def test_bypass_reports_no_fields(self):
        model = _model(
            _ds(_field("phone", mask="partial")),
            masking=MaskingPolicy(bypass_scopes=["pii"]),
        )
        _, report = _masker().apply(
            [["13812348888"]], ["phone"], model=model,
            principal=Principal(subject="7", scopes=frozenset({"pii"})),
        )
        assert report == {"fields": {}, "bypass": True}

    def test_report_is_plain_json(self):
        model = _model(_ds(_field("phone", mask="partial")))
        _, report = _masker().apply([["13812348888"]], ["phone"], model=model,
                                    principal=Principal(subject="1"))
        json.dumps(report)


# ── 调用方的数据不被改写 ─────────────────────────────────────────


class TestCallerDataIsUntouched:
    def test_rows_and_inner_lists_are_not_mutated(self, salt_env):
        model = _model(_ds(_field("phone", mask="partial")))
        rows = [["13812348888", "sh"]]

        out, _ = _masker().apply(rows, ["phone", "city"], model=model,
                                 principal=Principal(subject="1"))

        assert rows == [["13812348888", "sh"]]
        assert out is not rows
        assert out[0] is not rows[0]

    def test_short_rows_are_skipped_not_crashed(self, salt_env):
        """行比列短(上游裁过列 / 坏行)不该让整批结果炸掉。"""
        model = _model(_ds(_field("phone", mask="partial")))
        out, report = _masker().apply([[]], ["phone"], model=model,
                                      principal=Principal(subject="1"))
        assert out == [[]]
        assert report["fields"] == {"phone": "partial"}

    def test_extra_values_beyond_columns_are_left_alone(self, salt_env):
        model = _model(_ds(_field("phone", mask="partial")))
        out, _ = _masker().apply([["13812348888", "extra"]], ["phone"], model=model,
                                 principal=Principal(subject="1"))
        assert out == [["138****8888", "extra"]]

    def test_rows_are_passed_by_keyword_only(self):
        """签名是 ``apply(rows, columns, *, ...)`` —— 位置参数传错要立刻炸。"""
        with pytest.raises(TypeError):
            _masker().apply([["x"]], ["c"], _model(), Principal(subject="1"))
