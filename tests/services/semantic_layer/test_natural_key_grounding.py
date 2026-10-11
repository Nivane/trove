"""E4 · 自然键接地断言(本体形态批次,设计 §5.2)。

「键值是对象身份的锚」这条纪律的可执行形式:已声明自然键上的**等值**过滤,
字面量必须逐字来自提问(大小写无关)。编出来的键值(问张伟、答李四)不是
生成侧能自证对错的量 —— 由提问定,且由确定性断言拦在**执行前**。

三条兼容安全带(未声明 → 逐字节一致,单独钉):

* ``contract.natural_keys`` 为空(未声明实体 / 旧 wire / 现抽契约)→
  断言整体缺席;
* 只认 ``eq``(范围/集合/模式字面量另有语义,``or`` 是跨列复合);
* RLS 谓词整条豁免(字面量来自主体,不来自提问)。
"""
from __future__ import annotations

from trove.services.semantic_layer.compiler import (
    SemanticCompiler,
    _declared_natural_keys,
    natural_key_literals_grounded,
)
from trove.services.semantic_layer.contract import (
    PlanContract,
    contract_from_wire,
    contract_to_wire,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticEntity,
    SemanticField,
    SemanticMetric,
    SemanticModel,
)


def _field(name: str, datatype: str | None = None) -> SemanticField:
    return SemanticField(name=name, expression=name, datatype=datatype)


def _model(*, entity: SemanticEntity | None = None,
           pk: list[str] | None = None) -> SemanticModel:
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(
                name="client",
                primary_key=pk if pk is not None else ["client_id"],
                fields=[_field("client_id"), _field("name"),
                        _field("region")],
                entity=entity,
            ),
            SemanticDataset(name="plain", primary_key=["pid"],
                            fields=[_field("pid"), _field("name")]),
        ],
        metrics=[SemanticMetric("client count", "COUNT(client.client_id)",
                                datasets=["client"])],
    )


def _contract(keys, **kw) -> PlanContract:
    return PlanContract(skeleton_sql="SELECT 1", natural_keys=tuple(keys), **kw)


# ── ① 声明侧:哪些列算自然键(缺席容忍)────────────────────────────


def test_declared_natural_keys_only_from_declared_entities():
    """只收 entity 已声明的数据集;未声明一个都不收(断言整体缺席)。"""
    assert _declared_natural_keys(_model()) == ()
    assert _declared_natural_keys(None) == ()


def test_declared_natural_keys_use_declared_list():
    m = _model(entity=SemanticEntity(display_name="客户",
                                     natural_keys=["name"]))
    assert _declared_natural_keys(m) == (("client", "name"),)


def test_declared_natural_keys_fall_back_to_primary_key():
    """natural_keys 缺省 → primary_key(与 object_graph 投影同一读侧纪律)。"""
    m = _model(entity=SemanticEntity(display_name="客户"))
    assert _declared_natural_keys(m) == (("client", "client_id"),)


def test_declared_natural_keys_are_lowercased_and_ordered():
    """折小写(与解析器列形状同口径)+ 声明顺序(确定性,与 hash 无关)。"""
    m = SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="Client", primary_key=[],
                            fields=[_field("Id")],
                            entity=SemanticEntity(natural_keys=["Id", "Name"])),
            SemanticDataset(name="plain", primary_key=["pid"],
                            fields=[_field("pid")]),
        ],
    )
    assert _declared_natural_keys(m) == (("client", "id"), ("client", "name"))


def test_declared_natural_keys_skip_entity_with_no_keys_at_all():
    """entity 已声明但 natural_keys 与 primary_key 都空 → 不贡献列。"""
    m = _model(entity=SemanticEntity(display_name="客户"), pk=[])
    assert _declared_natural_keys(m) == ()


# ── ② 断言本体:正反例 ────────────────────────────────────────────


def test_grounded_literal_passes():
    """正例(施工单):提问含「张伟」+ 条件 name='张伟' → 通过。"""
    ok, why = natural_key_literals_grounded(
        _contract([("client", "name")]),
        "SELECT client.client_id FROM client WHERE client.name = '张伟'",
        "张伟的贷款有多少",
    )
    assert (ok, why) == (True, "")


def test_invented_literal_is_blocked():
    """反例(施工单):条件 name='李四' 与提问对不上 → 拦。"""
    ok, why = natural_key_literals_grounded(
        _contract([("client", "name")]),
        "SELECT client.client_id FROM client WHERE client.name = '李四'",
        "张伟的贷款有多少",
    )
    assert ok is False
    assert "client.name" in why and "'李四'" in why


def test_literal_match_is_case_insensitive():
    ok, _ = natural_key_literals_grounded(
        _contract([("client", "name")]),
        "SELECT client.client_id FROM client WHERE client.name = 'ZHANG WEI'",
        "loans of Zhang Wei",
    )
    assert ok is True


def test_literal_substring_of_question_is_enough():
    ok, _ = natural_key_literals_grounded(
        _contract([("client", "name")]),
        "SELECT client.client_id FROM client WHERE client.name = 'Zhang'",
        "Zhang Wei's loans",
    )
    assert ok is True


def test_unqualified_generated_column_still_asserted():
    """生成侧写裸列(去掉表限定)→ 仍按列名命中断言,不因写法放行。"""
    ok, why = natural_key_literals_grounded(
        _contract([("client", "name")]),
        "SELECT client_id FROM client WHERE name = '李四'",
        "张伟的贷款有多少",
    )
    assert ok is False and "name" in why


def test_column_on_another_table_not_asserted():
    """限定到别的表 → 不是这条自然键的过滤(不打回)。"""
    ok, _ = natural_key_literals_grounded(
        _contract([("client", "name")]),
        "SELECT other.id FROM other WHERE other.name = '李四'",
        "张伟的贷款有多少",
    )
    assert ok is True


# ── ③ 只认 eq;RLS 豁免;缺席容忍 ──────────────────────────────────


def test_range_equality_is_not_asserted():
    """范围/集合/模式字面量另有语义(`> 5` / `IN (...)` / `LIKE`)→ 不看。"""
    for sql in (
        "SELECT client.client_id FROM client WHERE client.name > '李四'",
        "SELECT client.client_id FROM client WHERE client.name IN ('李四', '王五')",
        "SELECT client.client_id FROM client WHERE client.name LIKE '李%'",
    ):
        ok, why = natural_key_literals_grounded(
            _contract([("client", "name")]), sql, "张伟的贷款有多少")
        assert (ok, why) == (True, ""), sql


def test_or_composite_is_not_asserted_but_its_leaves_are():
    """析取复合节点(op=or)不逐字断言,但析取**叶子**是独立的等值条件。

    键值「必须来自提问」与连接词无关:问张伟、SQL 里写着
    ``name='李四' OR region='R1'``,李四同样是编出来的 —— 该拦。
    """
    contract = _contract([("client", "name")])
    grounded = "SELECT client.client_id FROM client WHERE client.name = '张伟' OR client.region = 'R1'"
    assert natural_key_literals_grounded(contract, grounded, "张伟的贷款有多少") == (True, "")
    invented = "SELECT client.client_id FROM client WHERE client.name = '李四' OR client.region = 'R1'"
    ok, why = natural_key_literals_grounded(contract, invented, "张伟的贷款有多少")
    assert ok is False and "'李四'" in why


def test_rls_row_filter_literal_is_exempt():
    """RLS 谓词的字面量来自主体(登录身份),不来自提问 → 整条豁免。"""
    contract = _contract(
        [("client", "client_id")],
        row_filters=(((("client", "client_id"),), "eq", ("C1",)),),
    )
    ok, why = natural_key_literals_grounded(
        contract,
        "SELECT client.client_id FROM client WHERE client.client_id = 'C1'",
        "所有客户有多少",
    )
    assert (ok, why) == (True, "")


def test_rls_exemption_does_not_leak_to_other_conditions():
    """豁免只覆盖与 RLS 谓词同形状的那条;别的编造值照拦。"""
    contract = _contract(
        [("client", "client_id"), ("client", "name")],
        row_filters=(((("client", "client_id"),), "eq", ("C1",)),),
    )
    ok, _ = natural_key_literals_grounded(
        contract,
        "SELECT client.client_id FROM client WHERE client.client_id = 'C1' "
        "AND client.name = '李四'",
        "张伟的贷款有多少",
    )
    assert ok is False


def test_undeclared_entity_skips_the_assertion():
    """未声明 entity(契约无自然键)→ 断言缺席:与现状逐字节一致。"""
    ok, why = natural_key_literals_grounded(
        _contract([]),
        "SELECT client.client_id FROM client WHERE client.name = '李四'",
        "张伟的贷款有多少",
    )
    assert (ok, why) == (True, "")


def test_empty_question_skips_the_assertion():
    ok, _ = natural_key_literals_grounded(
        _contract([("client", "name")]),
        "SELECT client.client_id FROM client WHERE client.name = '李四'",
        "",
    )
    assert ok is True


def test_unparseable_generated_blocks():
    """解析不了 → 打回(与 skeleton_preserved 同款:谈不上保真)。"""
    ok, why = natural_key_literals_grounded(
        _contract([("client", "name")]), "NOT A QUERY !!!", "张伟")
    assert ok is False and "unparseable" in why


def test_non_select_generated_blocks():
    ok, why = natural_key_literals_grounded(
        _contract([("client", "name")]), "UPDATE client SET name = 'x'", "张伟")
    assert ok is False and "not a SELECT" in why


def test_empty_generated_skips():
    ok, _ = natural_key_literals_grounded(
        _contract([("client", "name")]), "", "张伟")
    assert ok is True


# ── ④ wire 往返 + 兼容安全带(旧 wire 缺键)──────────────────────


def test_wire_round_trip_preserves_natural_keys():
    contract = _contract([("client", "client_id"), ("loan", "loan_id")])
    decoded = contract_from_wire(contract_to_wire(contract))
    assert decoded == contract
    assert decoded.natural_keys == (("client", "client_id"), ("loan", "loan_id"))


def test_wire_natural_keys_shape_is_json_pairs():
    wire = contract_to_wire(_contract([("client", "client_id")]))
    assert wire["natural_keys"] == [["client", "client_id"]]


def test_old_wire_without_the_key_defaults_to_empty():
    """旧 checkpoint 的 wire 没有这个键 → 空元组 → 断言缺席(逐字节一致)。"""
    decoded = contract_from_wire({"skeleton_sql": "SELECT 1"})
    assert decoded == PlanContract(skeleton_sql="SELECT 1")
    assert decoded.natural_keys == ()


def test_malformed_natural_keys_wire_yields_none():
    """形状异常 → 整份契约作废(不是解出能解的部分 —— 那会削弱校验)。"""
    assert contract_from_wire(
        {"skeleton_sql": "SELECT 1", "natural_keys": "client"}) is None
    assert contract_from_wire(
        {"skeleton_sql": "SELECT 1", "natural_keys": [["client"]]}) is None
    assert contract_from_wire(
        {"skeleton_sql": "SELECT 1", "natural_keys": [["client", "id", "x"]]}) is None


# ── ⑤ 编译接线:模型声明真的进了契约(否则断言在生产里永不触发)──


def test_compile_fills_natural_keys_from_the_model():
    plan = {"aggregation": "count(client.client_id)",
            "answer_columns": ["count(client.client_id)"]}
    model = _model(entity=SemanticEntity(display_name="客户",
                                         natural_keys=["client_id"]))
    result = SemanticCompiler(model).compile_from_plan(plan, ["client"])
    assert result is not None
    assert result.contract.natural_keys == (("client", "client_id"),)
    # 未声明实体 → 契约不带自然键(兼容安全带在编译侧的落点)
    plain = SemanticCompiler(_model()).compile_from_plan(plan, ["client"])
    assert plain is not None and plain.contract.natural_keys == ()


def test_compiled_contract_wire_carries_natural_keys():
    """编译产物过 wire 往返(检查点通道)后断言仍可用。"""
    plan = {"aggregation": "count(client.client_id)",
            "answer_columns": ["count(client.client_id)"]}
    model = _model(entity=SemanticEntity(natural_keys=["client_id"]))
    result = SemanticCompiler(model).compile_from_plan(plan, ["client"])
    assert result is not None
    restored = contract_from_wire(contract_to_wire(result.contract))
    assert restored.natural_keys == (("client", "client_id"),)
    ok, why = natural_key_literals_grounded(
        restored, result.sql + " AND client.client_id = 'C9'", "all clients")
    assert ok is False and "client_id" in why
