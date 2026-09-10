"""Phase A0 —— 编译器交接契约(PlanContract)的形状、渲染与跨节点传输。

三条断言各自锁一件事:

1. **纯接口替换**:``render_contract`` 的输出与改造前 CompileResult/PartialCompile
   里硬编码的字符串**逐字相同**(golden)。它是一次性证明,证明 A0 没夹带
   行为变化;A1 之后可删。
2. **传输无损**:契约要跨节点边界,过 LangGraph 的 checkpointer。用**真实
   serde**(``JsonPlusSerializer``,与仓库实际 saver 同一份实现)做往返,并
   守住 wire 形状只含 JSON 标量/容器 —— 实测 frozenset 套元组会被静默清空成
   ``None``,自定义类型会被拦,这条守卫就是防止有人"顺手"改回去。
3. **抽取非空**:编译器真的填了 join 边/过滤/分组宽度。否则前两条再漂亮也是
   空转(全空契约永远"保真")。
"""

from __future__ import annotations

import pytest

from trove.services.semantic_layer.contract import (
    PlanContract,
    contract_from_wire,
    contract_to_wire,
    render_contract,
)
from tests.services.semantic_layer.test_compiler_sql import _compile

_SQL = "SELECT district.A3\nFROM loan"

# 改造前 compiler.py 里硬编码的两段文案(取自 A0 之前的提交,逐字誊写)。
_PRE_A0_FULL = (
    "Compiled SQL (authoritative — generate exactly this SQL; only "
    "fix dialect or formatting if the schema demands it):\n"
    "```sql\nSELECT district.A3\nFROM loan\n```"
)
_PRE_A0_PARTIAL = (
    "Compiled skeleton (authoritative — preserve these joins, filters and "
    "grouping exactly; only fill the gaps below):\n"
    "```sql\nSELECT district.A3\nFROM loan\n```\n"
    "Components NOT covered by the semantic model — generate these "
    "yourself from the query plan:\n"
    "- enum_value_unresolved: client.gender\n"
    "- no_metric_match"
)


class TestRenderIsPureInterfaceSwap:
    """渲染输出与 A0 之前逐字一致 —— 证明只换了接口形状。"""

    def test_full_compile_block_is_byte_identical(self):
        assert render_contract(PlanContract(skeleton_sql=_SQL)) == _PRE_A0_FULL

    def test_partial_block_is_byte_identical(self):
        contract = PlanContract(
            skeleton_sql=_SQL,
            partial=True,
            gaps=(
                {"reason": "enum_value_unresolved", "component": "client.gender"},
                # 无 component 的缺口:改造前走 suffix="" 分支,不能多出 ": "
                {"reason": "no_metric_match", "component": ""},
            ),
        )
        assert render_contract(contract) == _PRE_A0_PARTIAL

    def test_full_compile_omits_gap_section(self):
        """非 partial 不渲染缺口段 —— 即使 gaps 非空(形状上不变量)。"""
        contract = PlanContract(
            skeleton_sql=_SQL, gaps=({"reason": "no_metric_match"},))
        assert render_contract(contract) == _PRE_A0_FULL


class TestWireTransport:
    """契约跨节点边界:LangGraph 每步都把 state 交给 checkpointer 序列化。"""

    @staticmethod
    def _sample() -> PlanContract:
        return PlanContract(
            skeleton_sql="SELECT d.A3, AVG(l.amount)\nFROM loan l",
            join_edges=(
                (("account", "account_id"), ("loan", "account_id")),
                (("account", "district_id"), ("district", "district_id")),
            ),
            where=(( (("district", "a3"),), "eq", ("Prague",)),),
            group_by_width=1,
            gaps=({"reason": "no_metric_match", "component": "loan.amount"},),
            partial=True,
        )

    def test_round_trip_is_lossless(self):
        contract = self._sample()
        assert contract_from_wire(contract_to_wire(contract)) == contract

    def test_round_trip_through_the_real_checkpointer_serde(self):
        """经 langgraph 的 JsonPlusSerializer(与仓库 saver 同一实现)往返。

        这里刻意不用 pytest 的等价断言代替:上一版把 ``_skeleton_*`` 的返回
        形状(``set[frozenset[tuple]]``)直接搬上 state,serde 会把
        ``frozenset({('a','x')})`` 静默解成 ``None`` —— 校验读到空结构就
        "全部保真"。这个测试就是那条静默丢失的回归。
        """
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        serde = JsonPlusSerializer()
        contract = self._sample()
        payload = {"contract": contract_to_wire(contract)}
        restored = serde.loads_typed(serde.dumps_typed(payload))
        assert restored == payload, "wire 形状跨不过 checkpointer"
        assert contract_from_wire(restored["contract"]) == contract

    def test_serde_would_destroy_the_native_extractor_shape(self):
        """反证:抽取器的原生形状(frozenset 套元组)过 serde 就没了。

        这不是"契约必须绕开"的教条,是那个静默丢失的**可执行证据** ——
        哪天 langgraph 修好了,这条会红,那时才可以把 wire 转换删掉。
        """
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        serde = JsonPlusSerializer()
        native = frozenset({("account", "account_id"), ("loan", "account_id")})
        assert serde.loads_typed(serde.dumps_typed(native)) is None

    def test_wire_holds_only_json_scalars_and_containers(self):
        """形状守卫:wire 里不许出现 tuple/set/frozenset/自定义类型。

        tuple 会被序列化成 list(类型变了)、set/frozenset 会被清空、
        自定义类型会被严格模式拦截 —— 三者都别进 state。
        """
        wire = contract_to_wire(self._sample())

        def walk(node, path="$"):
            assert not isinstance(node, (tuple, set, frozenset)), (
                f"{path} 是非 JSON 容器 {type(node).__name__};"
                " 过 checkpointer 会丢内容或改类型"
            )
            assert node is None or isinstance(node, (str, int, bool, list, dict)), (
                f"{path} 是自定义类型 {type(node).__name__}:checkpointer 拦"
            )
            if isinstance(node, dict):
                for k, v in node.items():
                    assert isinstance(k, str), f"{path} 的键必须是 str"
                    walk(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{path}[{i}]")

        walk(wire)

    @pytest.mark.parametrize("bad", [
        None,
        {},
        {"skeleton_sql": ""},                       # 空 SQL:契约无意义
        {"skeleton_sql": "SELECT 1", "join_edges": "not-a-list"},
        {"skeleton_sql": "SELECT 1", "join_edges": [[["a", "b"]]]},   # 边只有一端
        {"skeleton_sql": "SELECT 1", "where": [{"cols": "x", "op": "eq", "values": []}]},
        {"skeleton_sql": "SELECT 1", "gaps": [["not", "a", "dict"]]},
        {"skeleton_sql": "SELECT 1", "group_by_width": -1},
        {"skeleton_sql": "SELECT 1", "group_by_width": "2"},
    ])
    def test_malformed_wire_yields_none_not_a_partial_contract(self, bad):
        """形状异常 → None(没有契约),**不是**"解出能解的部分"。

        部分解出的契约 = 被削弱的校验(join 边少了几条)且看不出来,那是
        fail-open。None 是明确信号,调用方退回老路径,松紧与契约缺席前一致。
        """
        assert contract_from_wire(bad) is None

    def test_missing_optional_keys_default_to_empty(self):
        """只有 skeleton_sql 也能还原 —— 全量编译没有缺口/结构是常态。"""
        contract = contract_from_wire({"skeleton_sql": "SELECT 1"})
        assert contract == PlanContract(skeleton_sql="SELECT 1")


class TestCompilerPopulatesContract:
    """编译器真的把结构填进去了(否则上面全是空转)。"""

    def test_join_filter_grouping_are_captured(self):
        plan = {
            "tables": ["loan", "district", "account"],
            "aggregation": "avg(loan.amount)",
            "answer_columns": ["district.A3", "avg(loan.amount)"],
            "conditions": [{"field": "district.A3", "op": "=", "value": "Prague"}],
        }
        result = _compile(plan, ["loan", "district", "account"])
        assert result is not None
        contract = result.contract
        assert contract.partial is False
        assert contract.skeleton_sql == result.sql
        assert {("account", "account_id"), ("loan", "account_id")} in [
            set(edge) for edge in contract.join_edges
        ]
        assert {("account", "district_id"), ("district", "district_id")} in [
            set(edge) for edge in contract.join_edges
        ]
        assert ( (("district", "a3"),), "eq", ("Prague",) ) in contract.where
        assert contract.group_by_width == 1
        assert contract.gaps == ()

    def test_partial_contract_carries_gaps_and_marks_itself(self):
        from trove.services.semantic_layer.compiler import PartialCompile
        from tests.services.semantic_layer.test_compiler_sql import _client_model

        # 未归一的枚举值 → 软 MISS:骨架留下度量,过滤值交生成通道补
        plan = {
            "tables": ["client"],
            "aggregation": "number of clients",
            "answer_columns": ["number of clients"],
            "conditions": [{"field": "client.gender", "op": "=", "value": "x"}],
        }
        from trove.services.semantic_layer.compiler import SemanticCompiler

        # 走 compile_detailed:软 MISS 在 compile_from_plan(旧契约)上按
        # None 回报告,只有 detailed 才返回骨架对象。
        result = SemanticCompiler(_client_model()).compile_detailed(plan, ["client"])
        assert isinstance(result, PartialCompile)
        assert result.contract.partial is True
        # gaps 与 miss_parts 同源同形(学习/归因消费的是同一份数据)
        assert result.contract.gaps == tuple(result.miss_parts)
        assert "enum_value_unresolved" in render_contract(result.contract)

    def test_canonical_order_is_deterministic(self):
        """集合语义 → 有序表示:同一 plan 两次编译,字节级一致。

        (渲染要进 prompt，顺序不定会让同题两轮提示词不同,缓存与 golden
        都失去意义。)
        """
        plan = {
            "tables": ["loan", "district", "account"],
            "aggregation": "avg(loan.amount)",
            "answer_columns": ["district.A3", "avg(loan.amount)"],
            "conditions": [{"field": "district.A3", "op": "=", "value": "Prague"}],
        }
        a = _compile(plan, ["loan", "district", "account"]).contract
        b = _compile(plan, ["loan", "district", "account"]).contract
        assert a == b
        assert list(a.join_edges) == sorted(a.join_edges)
        assert render_contract(a) == render_contract(b)
