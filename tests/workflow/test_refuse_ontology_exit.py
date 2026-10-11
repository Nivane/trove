"""E5 拒绝出口消费(本体形态 O1):显示名文案 + related_links + 一键补链接。

FR-REFUSE-1..3 / 设计 §5.3 的四件事:
1. 关系类硬 MISS 的文案把表名换成**实体显示名**(只换已声明实体,插值
   边界不动——仍只有 component 稳定的两个 reason 插 {c});
2. refusal 兄弟键 ``related_links`` —— 本问涉及的表/实体间**已声明**的
   链接清单(数据即 ``build_object_graph`` 投影的过滤,零 I/O;键缺席 =
   投影不出来,[] = 投影出来是零条,两个可区分的事实);
3. ``new_relation_from_miss`` next_action —— 机器**确定**两端点时才给,
   列对仅在唯一 ``*_id`` 名称匹配时预填(机器不猜列,不自动落草稿);
4. 未声明实体/无模型的存量路径逐字节回退(测试钉死)。

前端的形状契约由 ``frontend/tests/refusal-links.spec.ts`` 同契约钉住
(条目 {name, from, to, display_name, cardinality},display_name 缺席回退
关系名)—— 本文件是后端侧的那一半。
"""

from __future__ import annotations

import pytest

from trove.core.config import AgentConfig
from trove.services.kb.service import KbService
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticEntity,
    SemanticField,
    SemanticModel,
    SemanticRelationship,
)
from trove.workflow.nodes.refuse import (
    _component_tables,
    _display_mapped_detail,
    _related_links,
    make_refuse,
)
from trove.workflow.state import WorkflowState


def _f(name: str) -> SemanticField:
    return SemanticField(name=name, expression=name)


def _entity_model() -> SemanticModel:
    """loan→贷款 / account→客户 / district→地区 + 两条已声明链接的样板模型。"""
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(
                name="loan", primary_key=["loan_id"],
                fields=[_f("loan_id"), _f("account_id"), _f("amount")],
                entity=SemanticEntity(display_name="贷款")),
            SemanticDataset(
                name="account", primary_key=["account_id"],
                fields=[_f("account_id"), _f("district_id")],
                entity=SemanticEntity(display_name="客户")),
            SemanticDataset(
                name="district", primary_key=["district_id"],
                fields=[_f("district_id")],
                entity=SemanticEntity(display_name="地区")),
        ],
        relationships=[
            # 声明了显示名与基数 → 投影逐字透出
            SemanticRelationship("loan_to_account", "loan", "account",
                                 from_columns=["account_id"],
                                 to_columns=["account_id"],
                                 cardinality="1:N", display_name="属于"),
            # 未声明显示名/基数 → 投影 D1 回退(列连接串,"" 原样)
            SemanticRelationship("account_to_district", "account", "district",
                                 from_columns=["district_id"],
                                 to_columns=["district_id"]),
        ],
    )


def _undeclared_model() -> SemanticModel:
    """无实体声明的存量形状(loan/account,与 test_refuse._demo_model 同款)。"""
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"],
                            fields=[_f("loan_id"), _f("account_id")]),
            SemanticDataset(name="account", primary_key=["account_id"],
                            fields=[_f("account_id"), _f("district_id")]),
        ],
        relationships=[
            SemanticRelationship("loan_to_account", "loan", "account",
                                 from_columns=["account_id"],
                                 to_columns=["account_id"]),
        ],
    )


class FakeProvider:
    enabled = True

    def __init__(self, model):
        self._model = model

    def model(self):
        return self._model


class ScriptedLLM:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = 0

    async def chat(self, model, messages, **kwargs):
        self.calls += 1
        return next(self._responses)


def make_state(**kwargs) -> WorkflowState:
    defaults = {"session_id": "s1", "question": "这些客户关联的贷款?", "lang": "zh"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


@pytest.fixture
def kb(tmp_path):
    return KbService(tmp_path / "proj")


def _node(model=None, responses=("not yaml at all",), kb=None):
    return make_refuse(
        ScriptedLLM(list(responses)),
        AgentConfig(target="mock/model"),
        kb=kb,
        semantic_layer=FakeProvider(model) if model is not None else None,
    )


def _relation_refusal(reason: str, component: str, *, tables=None) -> dict:
    plan: dict = {"aggregation": "SUM(loan.amount)"}
    if tables is not None:
        plan["tables"] = tables
    return {"reason": "uncovered", "question": "这些客户关联的贷款?",
            "plan": plan,
            "compile_miss": {"reason": reason, "component": component}}


# ── 1. 文案显示名替换 ────────────────────────────────────


class TestDisplayNameCopy:
    async def _run(self, kb, model, reason, component="loan, account", tables=None):
        node = _node(model, kb=kb)
        return await node(make_state(
            datasource="demo",
            refusal=_relation_refusal(reason, component, tables=tables)))

    async def test_unknown_cardinality_copy_maps_to_display_names(self, kb):
        out = await self._run(kb, _entity_model(), "unknown_cardinality")
        msg = out["clarification_question"]
        assert "贷款, 客户" in msg
        assert "loan" not in msg and "account" not in msg
        assert "基数未声明" in msg

    async def test_fan_out_copy_maps_to_display_names(self, kb):
        out = await self._run(kb, _entity_model(), "fan_out")
        msg = out["clarification_question"]
        assert "贷款, 客户" in msg
        assert "loan" not in msg and "account" not in msg

    async def test_undeclared_model_is_byte_identical(self, kb):
        """未声明实体 → 文案逐字节回退现状(表名原样)。"""
        out = await self._run(kb, _undeclared_model(), "unknown_cardinality")
        msg = out["clarification_question"]
        assert "基数未声明" in msg
        assert "loan, account" in msg
        assert "贷款" not in msg

    async def test_only_declared_tables_are_replaced(self, kb):
        """混合:一表已声明、一表未声明 → 只换已声明的那个。"""
        model = _entity_model()
        out = await self._run(kb, model, "unknown_cardinality",
                              component="loan, ghost")
        msg = out["clarification_question"]
        assert "基数未声明" in msg
        assert "贷款, ghost" in msg

    async def test_unreachable_and_ambiguous_copy_untouched(self, kb):
        """另两个关系类 reason 的 component 可能带内部诊断串:不插值也不替换
        (插值边界不动),显示名映射不得把诊断串改头换面。"""
        model = _entity_model()
        out = await self._run(
            kb, model, "unreachable_table",
            component="tables outside join tree: loan, account")
        msg = out["clarification_question"]
        assert "问题涉及的表" in msg
        for leak in ("tables outside", "join tree", "loan", "account", "贷款"):
            assert leak not in msg, msg
        out2 = await self._run(
            kb, model, "ambiguous_join_path",
            component="explicit joins reference undeclared edges")
        msg2 = out2["clarification_question"]
        assert "表之间唯一的关联路径" in msg2
        assert "explicit joins" not in msg2 and "贷款" not in msg2

    async def test_non_relation_reason_component_never_mapped(self, kb):
        """指标/字段类 component 与表同名也不替换({c} 有但不属于关系域)。"""
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal={"reason": "uncovered", "question": "q", "plan": {},
                     "compile_miss": {"reason": "no_metric_match",
                                      "component": "loan"}}))
        assert "loan" in out["clarification_question"]

    async def test_compile_miss_stays_raw_for_audit(self, kb):
        """显示名替换只活在文案里:refusal 的 compile_miss 保持原始分因
        (审计 miss_component / 报表 by_table 按原文聚合)。"""
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("unknown_cardinality", "loan, account")))
        assert out["refusal"]["compile_miss"] == {
            "reason": "unknown_cardinality", "component": "loan, account"}


class TestDisplayMappedDetailPure:
    def test_no_entity_model_returns_unchanged(self):
        detail = "unknown_cardinality: loan, account"
        assert _display_mapped_detail(detail, _undeclared_model()) == detail
        assert _display_mapped_detail(detail, None) == detail

    def test_non_c_reasons_pass_through(self):
        detail = "unreachable_table: tables outside join tree: loan, account"
        assert _display_mapped_detail(detail, _entity_model()) == detail

    def test_slug_only_detail_unchanged(self):
        assert _display_mapped_detail("unknown_cardinality", _entity_model()) \
            == "unknown_cardinality"

    def test_component_tables_tokenization(self):
        assert _component_tables("loan, account") == ["loan", "account"]
        assert _component_tables("tables outside join tree: loan, account") == \
            ["loan", "account"]
        # 内部诊断串(含空白的整句)不是标识符 → 丢弃,不当表名
        assert _component_tables("explicit joins reference undeclared edges") == []
        assert _component_tables("a b, loan") == ["loan"]
        assert _component_tables("Loan, loan,  ACCOUNT ") == ["Loan", "ACCOUNT"]
        assert _component_tables('`loan`, "account"') == ["loan", "account"]
        assert _component_tables("") == []


# ── 2. related_links 兄弟键 ──────────────────────────────


LINK_KEYS = {"name", "from", "to", "display_name", "cardinality"}


class TestRelatedLinks:
    async def test_links_between_involved_tables_only(self, kb):
        """两端都在涉及集里的已声明链接逐条透出;条目形状 = 前端契约。"""
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("unknown_cardinality", "loan, account")))
        links = out["refusal"]["related_links"]
        assert links == [{
            "name": "loan_to_account",
            "from": "贷款",          # 端点对象显示名(D1:实体名缺省回退数据集名)
            "to": "客户",
            "display_name": "属于",   # 声明优先
            "cardinality": "1:N",
        }]
        assert set(links[0]) == LINK_KEYS

    async def test_endpoint_outside_involved_set_excluded(self, kb):
        """account→district 链接:district 不在涉及集 → 不列。"""
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("unknown_cardinality", "loan, account")))
        names = [l["name"] for l in out["refusal"]["related_links"]]
        assert names == ["loan_to_account"]

    async def test_plan_tables_widen_the_involved_set(self, kb):
        """component 单表 + plan.tables 补出另一端 → 链接仍列得出来。"""
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("unknown_cardinality", "loan",
                                      tables=["loan", "account"])))
        names = [l["name"] for l in out["refusal"]["related_links"]]
        assert names == ["loan_to_account"]

    async def test_display_name_falls_back_to_projection_d1(self, kb):
        """未声明 display_name → 投影的列连接串(D1);基数 "" 原样透出。"""
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("unreachable_table",
                                      "tables outside join tree: account, district")))
        links = out["refusal"]["related_links"]
        assert links == [{
            "name": "account_to_district",
            "from": "客户",
            "to": "地区",
            "display_name": "district_id → district_id",
            "cardinality": "",
        }]

    async def test_empty_list_when_no_declared_link_in_direction(self, kb):
        """涉及表都在声明面上、方向内一条链接都没有 → [] 而不是缺席。"""
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("unreachable_table",
                                      "tables outside join tree: loan, district")))
        assert out["refusal"]["related_links"] == []

    async def test_ambiguous_bfs_component_lists_all_involved_links(self, kb):
        """BFS 变体的 component 是表名列表:列出涉及集内全部链接(条数 > 1)。"""
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("ambiguous_join_path",
                                      "loan, account, district")))
        names = [l["name"] for l in out["refusal"]["related_links"]]
        assert names == ["loan_to_account", "account_to_district"]

    async def test_absent_for_non_relation_reason(self, kb):
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal={"reason": "uncovered", "question": "q", "plan": {},
                     "compile_miss": {"reason": "no_metric_match",
                                      "component": "AVG(loan.amount)"}}))
        assert "related_links" not in out["refusal"]

    async def test_absent_for_unresolvable_involved_set(self, kb):
        """component 是内部诊断串且 plan 无表 → 投影不出「涉及谁」→ 键缺席
        (不摆一份空的断言;与「投影出来是零条」的 [] 是两个事实)。"""
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal={"reason": "uncovered", "question": "q", "plan": {},
                     "compile_miss": {
                         "reason": "ambiguous_join_path",
                         "component": "explicit joins reference undeclared edges"}}))
        assert "related_links" not in out["refusal"]

    async def test_absent_without_model(self, kb):
        """无已解析模型(无 semantic_layer)→ 键缺席,存量形状不变。"""
        node = _node(None, kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("unknown_cardinality", "loan, account")))
        assert "related_links" not in out["refusal"]

    def test_projection_is_pure_and_zero_io(self):
        """纯函数面:同一输入两次调用逐字节同一输出。"""
        model = _entity_model()
        a = _related_links(model, component="loan, account", plan=None)
        b = _related_links(model, component="loan, account", plan=None)
        assert a == b
        assert _related_links(None, component="loan", plan=None) is None


# ── 3. new_relation_from_miss 一键补链接 ─────────────────


class TestNewRelationAction:
    async def test_action_prefills_endpoints_and_unique_id_columns(self, kb):
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("unknown_cardinality", "loan, account")))
        actions = out["refusal"]["next_actions"]
        assert len(actions) == 1
        action = actions[0]
        assert action["id"] == action["kind"] == "new_relation_from_miss"
        assert action["href"] == "/admin/semantic?ds=demo"
        assert action["admin_only"] is True
        assert action["label"] == "声明关系（已预填两端）"
        assert action["payload"] == {
            "draft_kind": "relationship",
            "from_dataset": "loan",
            "to_dataset": "account",
            "reason": "unknown_cardinality",
            "from_columns": ["account_id"],
            "to_columns": ["account_id"],
        }
        assert set(action) <= {"id", "kind", "label", "href", "admin_only", "payload"}

    async def test_action_replaces_open_semantic(self, kb):
        """原「去语义工作台」出口被预填版取代(同一落点,不并存)。"""
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("fan_out", "loan, account")))
        kinds = [a["kind"] for a in out["refusal"]["next_actions"]]
        assert kinds == ["new_relation_from_miss"]

    async def test_draft_derived_actions_survive(self, kb):
        """草稿类出口(已落 pending 的确认)是另一件已发生的事,照留。"""
        yaml = """\
draft:
  kind: metric
  name: avg_loan_amount
  expression: AVG(loan.amount)
  synonyms: [平均贷款金额]
  datasets: [loan]
"""
        node = _node(_entity_model(), responses=(yaml,), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("unknown_cardinality", "loan, account")))
        kinds = [a["kind"] for a in out["refusal"]["next_actions"]]
        assert kinds == ["new_relation_from_miss", "confirm_draft"]

    async def test_reason_is_the_raw_miss_slug(self, kb):
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("unreachable_table",
                                      "tables outside join tree: loan, account")))
        action = out["refusal"]["next_actions"][0]
        assert action["kind"] == "new_relation_from_miss"
        assert action["payload"]["reason"] == "unreachable_table"

    async def test_columns_left_empty_when_match_not_unique(self, kb):
        """两个共享 *_id 名 → 挑哪个都是猜 → 不预填列(机器不猜列)。"""
        model = SemanticModel(
            name="fin",
            datasets=[
                SemanticDataset(name="loan", primary_key=["loan_id"],
                                fields=[_f("loan_id"), _f("account_id"),
                                        _f("customer_id")]),
                SemanticDataset(name="account", primary_key=["account_id"],
                                fields=[_f("account_id"), _f("customer_id")]),
            ],
        )
        node = _node(model, kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("unknown_cardinality", "loan, account")))
        action = out["refusal"]["next_actions"][0]
        assert action["kind"] == "new_relation_from_miss"
        assert set(action["payload"]) == {
            "draft_kind", "from_dataset", "to_dataset", "reason"}

    async def test_no_action_when_more_than_two_tables(self, kb):
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("ambiguous_join_path",
                                      "loan, account, district")))
        kinds = [a["kind"] for a in out["refusal"]["next_actions"]]
        assert kinds == ["open_semantic"]

    async def test_no_action_when_endpoint_undeclared(self, kb):
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("unknown_cardinality", "loan, ghost")))
        kinds = [a["kind"] for a in out["refusal"]["next_actions"]]
        assert kinds == ["open_semantic"]

    async def test_no_action_without_datasource(self, kb):
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="",
            refusal=_relation_refusal("unknown_cardinality", "loan, account")))
        kinds = [a["kind"] for a in out["refusal"]["next_actions"]]
        assert kinds == ["open_semantic"]

    async def test_english_label(self, kb):
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo", lang="en",
            refusal=_relation_refusal("unknown_cardinality", "loan, account")))
        assert out["refusal"]["next_actions"][0]["label"] == \
            "Declare the relationship (both ends prefilled)"

    async def test_blueprint_never_auto_lands_a_draft(self, kb):
        """一键补链接 = 打开预填对话框,不是自动落草稿(A2 决策 (d):
        错基数的自动关系 = 静默错结果;确认门照旧)。"""
        from trove.services.semantic_layer.manage import SemanticManager
        node = _node(_entity_model(), kb=kb)
        out = await node(make_state(
            datasource="demo",
            refusal=_relation_refusal("unknown_cardinality", "loan, account")))
        assert out["refusal"]["draft"] is None
        assert out["refusal"]["draft_entry"] is None
        assert SemanticManager(kb).drafts("demo")["pending"] == []
