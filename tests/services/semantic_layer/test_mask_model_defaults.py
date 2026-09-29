"""脱敏声明的**惰性缺省**(设计 §6.1 / A11)。

新字段的缺省值决定存量文档的行为:两个缺省必须都是「什么都不做」——
``mask=""`` 与 ``MaskingPolicy()`` 都指向「没声明过脱敏」。这些断言看着
琐碎,但它们锁的是 A11「存量语义模型无 ``mask`` → 行为不变」的第一道口:
缺省一旦变成 ``partial`` 之类的非惰性值,存量模型会在无人察觉的情况下
开始改写结果。
"""

from trove.services.semantic_layer.models import (
    MaskingPolicy,
    SemanticField,
    SemanticModel,
)


def test_a_model_without_a_masking_block_is_inert():
    m = SemanticModel()

    assert m.masking.default_policy == "apply"
    assert m.masking.bypass_scopes == []
    assert m.masking.hash_salt_ref == ""


def test_a_field_without_a_mask_declaration_is_inert():
    f = SemanticField(name="phone", expression="phone")

    assert f.mask == ""


def test_each_model_gets_its_own_policy_object():
    """可变的 list 字段不能是共享缺省 —— 一处 append 会污染所有模型。"""
    a, b = SemanticModel(), SemanticModel()

    a.masking.bypass_scopes.append("pii")

    assert b.masking.bypass_scopes == []
    assert isinstance(b.masking, MaskingPolicy)
