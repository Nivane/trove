"""字段级脱敏**声明层**的解析与序列化(设计 §5.5 / §6.1 / §12 A11)。

声明分两处:
  - 字段级 ``mask``(值域 ``"" | none | partial | hash | null``);
  - 模型级 ``masking`` 块(``default_policy`` / ``bypass_scopes`` /
    ``hash_salt_ref``)。

**落点判断**:解析器读的模型级声明(``time_spine`` / ``ai_context`` /
``custom_extensions``)全挂在 ``semantic_model[0]`` 下,``masking`` 随它 ——
设计稿 §5.5 注释里的「semantics.yml 顶层」是**单模型文件**的松散说法,
文档顶层只有一个 ``version`` 是文档级键。这里不认文档顶层的 ``masking``
(同一件事两个写法必然要配一条优先级规则,而静默忽略 = 静默不脱敏,
所以在 lint 侧把这种写法报出来,见 tests/services/kb/test_lint.py)。

本文件锁三件事:解析落点、序列化不丢、以及**存量模型**(R6 之前写的、
没有这两个键的)解析后 ``mask`` 恒为 ``""``(A11)。
"""

import logging
from pathlib import Path

import pytest
import yaml

from trove.services.semantic_layer.manage import _field_to_dict, _model_to_dict
from trove.services.semantic_layer.models import MaskingPolicy, SemanticModel
from trove.services.semantic_layer.ossie import parse_ossie

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: 存量语义模型的真实样本(版本化在 git,至今未声明任何脱敏)。
_STOCK_MODELS = ["demo", "financial", "mysql_fin"]

MASKED_YAML = """
version: 0.2.0.dev0
semantic_model:
  - name: crm
    masking:
      default_policy: apply
      bypass_scopes:
        - pii
      hash_salt_ref: "env:TROVE_MASK_SALT"
    datasets:
      - name: customers
        source: crm.customers
        fields:
          - name: phone
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: phone
            mask: partial
          - name: id_card
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: id_card
            mask: hash
          - name: email
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: email
            mask: partial
"""

_PLAIN_YAML = """
semantic_model:
  - name: crm
    datasets:
      - name: customers
        source: crm.customers
        fields:
          - name: phone
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: phone
"""


def _masks(model: SemanticModel) -> list[tuple[str, str]]:
    return [(f.name, f.mask) for d in model.datasets for f in d.fields]


def _declared_mask_yaml(value: str) -> str:
    """一份只声明一个字段 ``mask: <value>`` 的最小模型。"""
    return f"""
semantic_model:
  - name: crm
    datasets:
      - name: customers
        fields:
          - name: phone
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: phone
            mask: {value}
"""


class TestFieldMaskParsing:
    def test_declared_masks_are_parsed(self):
        model = parse_ossie(MASKED_YAML, preferred_dialect="sqlite")

        assert _masks(model) == [
            ("phone", "partial"), ("id_card", "hash"), ("email", "partial")]

    @pytest.mark.parametrize("value", ["none", "partial", '"null"'])
    def test_every_inert_and_masked_mode_survives_parsing(self, value):
        model = parse_ossie(_declared_mask_yaml(value), preferred_dialect="sqlite")

        # 'null' 模式必须**带引号**写:裸 null 是 YAML 空值(见
        # test_blank_mask_value_is_inert 与 TestLintMask)
        assert _masks(model) == [("phone", value.strip('"'))]

    def test_mask_value_is_normalised(self):
        """大小写与空白归一化 —— 解析结果只有一种拼写(比较/展示/往返无歧义),
        门禁按同一规则判,不会出现「门禁放过、运行时看不懂」的两处不一致。"""
        model = parse_ossie(_declared_mask_yaml('" HASH "'), preferred_dialect="sqlite")

        assert _masks(model) == [("phone", "hash")]

    def test_field_without_a_mask_key_is_inert(self):
        """A11:存量模型没有 mask 键 → 空串(= 不脱敏),不是任何有效果的模式。"""
        model = parse_ossie(_PLAIN_YAML, preferred_dialect="sqlite")

        assert _masks(model) == [("phone", "")]

    def test_blank_mask_value_is_inert(self):
        """裸 ``mask:``(YAML null)解析为空 —— 不脱敏。

        注意这是**失败方向**上的一处不对称:作者写 ``mask: null`` 本意多半是
        值域里的 ``null`` 模式(始终输出 None),而 YAML 把它解析成 None → 空串。
        解析器不去猜(猜成 ``null`` 会让「作者本来没写」也变成最强脱敏),
        改由 lint 把这种写法在写盘前拦下,见 TestLintMask。
        """
        model = parse_ossie("""
semantic_model:
  - name: crm
    datasets:
      - name: customers
        fields:
          - name: phone
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: phone
            mask:
""", preferred_dialect="sqlite")

        assert _masks(model) == [("phone", "")]


class TestMaskingBlockParsing:
    def test_masking_block_is_parsed(self):
        model = parse_ossie(MASKED_YAML, preferred_dialect="sqlite")

        assert model.masking.default_policy == "apply"
        assert model.masking.bypass_scopes == ["pii"]
        assert model.masking.hash_salt_ref == "env:TROVE_MASK_SALT"

    def test_missing_masking_block_keeps_inert_defaults(self):
        model = parse_ossie(_PLAIN_YAML, preferred_dialect="sqlite")

        assert model.masking == MaskingPolicy()
        assert model.masking.default_policy == "apply"
        assert model.masking.bypass_scopes == []
        assert model.masking.hash_salt_ref == ""

    def test_document_level_masking_is_ignored_with_a_warning(self, caplog):
        """文档顶层的 masking 不生效 —— 但必须出声,不允许静默不脱敏。"""
        text = """
masking:
  default_policy: bypass
semantic_model:
  - name: crm
    datasets: []
"""
        with caplog.at_level(
            logging.WARNING, logger="trove.services.semantic_layer.ossie"
        ):
            model = parse_ossie(text, preferred_dialect="sqlite")

        assert model.masking.default_policy == "apply"
        assert any("semantic_model[0]" in r.getMessage() for r in caplog.records)

    def test_invalid_default_policy_falls_back_to_apply(self, caplog):
        """非法 default_policy 回落 ``apply``(脱敏) —— 与 time_spine 同法:
        坏值出声 + 取安全的一侧。"""
        text = """
semantic_model:
  - name: crm
    masking:
      default_policy: bypasss
    datasets: []
"""
        with caplog.at_level(
            logging.WARNING, logger="trove.services.semantic_layer.ossie"
        ):
            model = parse_ossie(text, preferred_dialect="sqlite")

        assert model.masking.default_policy == "apply"
        assert any("default_policy" in r.getMessage() for r in caplog.records)

    def test_bypass_scopes_bare_string_is_one_scope(self):
        """``bypass_scopes: pii`` 是单元素列表,不是 ['p','i','i']。"""
        text = """
semantic_model:
  - name: crm
    masking:
      bypass_scopes: pii
    datasets: []
"""
        model = parse_ossie(text, preferred_dialect="sqlite")

        assert model.masking.bypass_scopes == ["pii"]

    def test_bypass_scopes_drops_blank_entries_and_survives_a_scalar(self):
        text = """
semantic_model:
  - name: crm
    masking:
      bypass_scopes:
        - pii
        - ""
        - "  "
    datasets: []
"""
        model = parse_ossie(text, preferred_dialect="sqlite")
        assert model.masking.bypass_scopes == ["pii"]

        bad = """
semantic_model:
  - name: crm
    masking:
      bypass_scopes: 5
    datasets: []
"""
        # 坏形状不得把整个模型解析炸掉(provider 会因此丢 last-known-good)
        assert parse_ossie(bad, preferred_dialect="sqlite").masking.bypass_scopes == []

    def test_scalar_masking_block_is_ignored(self):
        text = """
semantic_model:
  - name: crm
    masking: nope
    datasets: []
"""
        assert parse_ossie(text, preferred_dialect="sqlite").masking == MaskingPolicy()


class TestMaskSerialization:
    def test_field_dict_carries_mask(self):
        model = parse_ossie(MASKED_YAML, preferred_dialect="sqlite")

        out = [_field_to_dict(f) for f in model.datasets[0].fields]

        assert [d["mask"] for d in out] == ["partial", "hash", "partial"]

    def test_field_dict_writes_the_empty_value_too(self):
        """空值也照写(与 label / examples 一致):管理页要能区分
        「没有这个键」和「声明了不脱敏」。"""
        model = parse_ossie(_PLAIN_YAML, preferred_dialect="sqlite")

        assert _field_to_dict(model.datasets[0].fields[0])["mask"] == ""

    def test_model_dict_carries_the_masking_block(self):
        model = parse_ossie(MASKED_YAML, preferred_dialect="sqlite")

        assert _model_to_dict(model)["masking"] == {
            "default_policy": "apply",
            "bypass_scopes": ["pii"],
            "hash_salt_ref": "env:TROVE_MASK_SALT",
        }

    def test_model_dict_writes_inert_defaults_too(self):
        assert _model_to_dict(SemanticModel())["masking"] == {
            "default_policy": "apply",
            "bypass_scopes": [],
            "hash_salt_ref": "",
        }

    def test_model_dict_masking_is_json_yaml_safe(self):
        """bypass_scopes 必须是 list 的拷贝,不是内部可变对象的引用。"""
        model = parse_ossie(MASKED_YAML, preferred_dialect="sqlite")

        out = _model_to_dict(model)
        out["masking"]["bypass_scopes"].append("admin")

        assert model.masking.bypass_scopes == ["pii"]


class TestMaskRoundTrip:
    """parse → serialize → parse 不丢声明。

    序列化形状里只有 ``expression`` 是扁平的(管理页展示用),所以回灌时
    把 expression 重新包回 ``dialects``;``mask`` 与 ``masking`` 是原样
    回灌的 —— 它们正是本文件的被测载荷。
    """

    @staticmethod
    def _reparse(model: SemanticModel) -> SemanticModel:
        d = _model_to_dict(model)
        doc = {
            "semantic_model": [{
                "name": d["name"],
                "masking": d["masking"],
                "datasets": [
                    {
                        "name": ds["name"],
                        "fields": [
                            {
                                "name": f["name"],
                                "expression": {"dialects": [
                                    {"dialect": "ANSI_SQL",
                                     "expression": f["expression"]}]},
                                "mask": f["mask"],
                            }
                            for f in ds["fields"]
                        ],
                    }
                    for ds in d["datasets"]
                ],
            }]
        }
        return parse_ossie(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
            preferred_dialect="sqlite",
        )

    def test_mask_declarations_survive_the_round_trip(self):
        first = parse_ossie(MASKED_YAML, preferred_dialect="sqlite")

        second = self._reparse(first)

        assert second.masking == first.masking
        assert _masks(second) == _masks(first)

    def test_absent_masks_stay_inert_through_the_round_trip(self):
        first = parse_ossie(_PLAIN_YAML, preferred_dialect="sqlite")

        second = self._reparse(first)

        assert _masks(second) == [("phone", "")]
        assert second.masking == MaskingPolicy()


class TestStockModelsStayInert:
    """A11:仓库里现存的语义模型(没有 mask / masking 键)不受影响。"""

    @pytest.mark.parametrize("datasource", _STOCK_MODELS)
    def test_stock_model_declares_no_mask(self, datasource):
        path = _REPO_ROOT / ".trove" / "kb" / datasource / "semantics.yml"
        model = parse_ossie(path.read_text(encoding="utf-8"),
                            preferred_dialect="sqlite")

        assert model.datasets
        assert {mask for _, mask in _masks(model)} == {""}
        assert model.masking == MaskingPolicy()
