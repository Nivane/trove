"""E2 写入面:实体(entity)/ 关系(relationship)声明的草稿落库与确认。

草稿通道是本体声明的自动写入路径:``create_draft`` 只写
``semantic_drafts.yml``(pending),``confirm_draft`` 经 ``ChangeService``
合并进 ``semantics.yml``(单一真源)。本文件的四条主线:

1. **写盘前校验**:数据集未声明 / 实体归属不一致 / 空声明 / 关系端点或
   键对缺失 —— 全部响亮拒绝。YAML 是单一真源,写进去的必须是可解析、
   在模型里**可见**的声明(解析器对缺键对的关系整体丢弃,写进去也是
   不可见的声明,当场拒绝优于安静的 no-op);
2. **往返**:create → confirm → semantics.yml → ``parse_ossie`` 模型侧
   看得见(发送/接收一致);
3. **carryover**:upsert 不抹未覆盖的键;``fan_out`` 刻意不可由 payload
   设置(编译期安全豁免,只能手写);
4. **未确认不可见**:pending 草稿不改变任何读面(模型 / 对象投影)。

列名纪律:联表键刻意不用 ``*_id`` 命名约定 —— 约定命中的跨表列会另有一
条 lint 规则(FK 未声明关系),那是另一件事的测试面。
"""

from __future__ import annotations

import yaml
import pytest

from trove.services.kb.service import KbService
from trove.services.semantic_layer.changes import ChangeError
from trove.services.semantic_layer.issues import validate_draft
from trove.services.semantic_layer.manage import (
    _KINDS,
    SemanticManager,
    _apply_draft,
)
from trove.services.semantic_layer.object_graph import build_object_graph


def _doc(*, relationship: dict | None = None) -> dict:
    loan: dict = {
        "name": "loan", "source": "loan", "primary_key": ["loan_id"],
        "fields": [
            {"name": "loan_id", "datatype": "Integer",
             "expression": {"dialects": [{
                 "dialect": "ANSI_SQL", "expression": "loan_id"}]}},
            {"name": "amount", "datatype": "Decimal",
             "expression": {"dialects": [{
                 "dialect": "ANSI_SQL", "expression": "amount"}]}},
            {"name": "account_no", "datatype": "String",
             "expression": {"dialects": [{
                 "dialect": "ANSI_SQL", "expression": "account_no"}]}},
        ],
    }
    account: dict = {
        "name": "account", "source": "account",
        "fields": [
            {"name": "account_no", "datatype": "String",
             "expression": {"dialects": [{
                 "dialect": "ANSI_SQL", "expression": "account_no"}]}},
            {"name": "balance", "datatype": "Decimal",
             "expression": {"dialects": [{
                 "dialect": "ANSI_SQL", "expression": "balance"}]}},
        ],
    }
    entry: dict = {"name": "demo", "datasets": [loan, account], "metrics": []}
    if relationship is not None:
        entry["relationships"] = [relationship]
    return {"version": "0.2.0.dev0", **entry}


def _kb_with(tmp_path, doc: dict) -> KbService:
    k = KbService(tmp_path / "proj")
    ds_dir = k.kb_dir / "demo"
    ds_dir.mkdir(parents=True)
    (ds_dir / "semantics.yml").write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return k


@pytest.fixture
def kb(tmp_path):
    return _kb_with(tmp_path, _doc())


def _minimal(*datasets: dict) -> dict:
    """``_apply_draft`` 直接吃的**完整文档**(扁平 OSSIE 0.2 根)。

    必须带 ``version`` 与数据集声明:裸 ``{"datasets": [...]}`` 会被
    ``_model_of`` 当成缺省 → 新建空模型,所有数据集判定都落「未声明」。
    """
    return {"version": "0.2.0.dev0", "name": "demo",
            "datasets": list(datasets), "metrics": []}


def _semantics_doc(kb: KbService) -> dict:
    return yaml.safe_load(
        (kb.kb_dir / "demo" / "semantics.yml").read_text(encoding="utf-8"))


def _dataset_doc(kb: KbService, name: str = "loan") -> dict:
    model = _semantics_doc(kb)
    return next(d for d in model["datasets"] if d["name"] == name)


def _entity_of(kb: KbService, name: str = "loan"):
    model = SemanticManager(kb).model("demo")
    ds = next(d for d in model.datasets if d.name == name)
    return ds.entity


def _messages(result: dict) -> list[str]:
    """errors + warnings 的 message 全集。

    写盘路径错误的归类(code/severity)是 lint 守门的测试面;这一层只钉
    「拒绝理由可读且不丢」(未命中的串落 warning 也绝不丢弃,见 issues.py)。
    """
    return [i["message"] for i in result["errors"] + result["warnings"]]


# ── entity ───────────────────────────────────────────────


class TestEntityWriteFace:
    def test_unknown_kind_still_rejected(self):
        """白名单是闭集:非法 kind 仍然当场拒绝(新 kind 只是加进闭集)。"""
        assert {"entity", "relationship"} <= _KINDS
        with pytest.raises(ValueError, match="未知草稿类型"):
            _apply_draft(_minimal(),
                         {"kind": "widget", "action": "upsert", "name": "x",
                          "payload": {}}, None)

    async def test_create_draft_rejects_unknown_kind(self, kb):
        with pytest.raises(ValueError, match="kind 必须为"):
            await SemanticManager(kb).create_draft(
                "demo", "widget", "upsert", "x", payload={"a": 1})

    async def test_entity_round_trip(self, kb):
        """create → confirm → YAML → 模型侧:三键声明全存活。"""
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "entity", "upsert", "loan",
            payload={"dataset": "loan", "display_name": "贷款",
                     "natural_keys": ["loan_id"], "aliases": ["借款"]},
            note="q")
        # 确认前:草稿在 pending,模型里看不见(未确认 = 未声明)
        assert manager.drafts("demo")["pending"][0]["id"] == entry["id"]
        assert _entity_of(kb) is None

        await manager.confirm_draft("demo", entry["id"], dialect="sqlite")

        entity = _entity_of(kb)
        assert entity is not None
        assert entity.display_name == "贷款"
        assert entity.natural_keys == ["loan_id"]
        assert entity.aliases == ["借款"]
        # YAML 形状:entity 子块落盘,只含声明了的键
        assert _dataset_doc(kb)["entity"] == {
            "display_name": "贷款", "natural_keys": ["loan_id"],
            "aliases": ["借款"]}
        assert manager.drafts("demo")["pending"] == []
        assert manager.drafts("demo")["applied"][0]["status"] == "applied"

    async def test_entity_applies_to_the_named_dataset_only(self, kb):
        """确认作用于草稿 name 命名的数据集,邻居不动。"""
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "entity", "upsert", "loan",
            payload={"dataset": "loan", "display_name": "贷款"})
        await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        assert _entity_of(kb, "loan") is not None
        assert _entity_of(kb, "account") is None

    async def test_entity_upsert_overwrites_previous_declaration(self, kb):
        """以蓝本新建的修正草稿确认后**覆盖**旧声明(确认一条修正声明是
        有意义的路径 —— 与 agg_duplicate 的「落草稿=诱导重复」不同)。"""
        manager = SemanticManager(kb)
        first = await manager.create_draft(
            "demo", "entity", "upsert", "loan",
            payload={"dataset": "loan", "display_name": "旧名", "aliases": ["旧"]})
        await manager.confirm_draft("demo", first["id"], dialect="sqlite")
        second = await manager.create_draft(
            "demo", "entity", "upsert", "loan",
            payload={"dataset": "loan", "display_name": "新名"})
        await manager.confirm_draft("demo", second["id"], dialect="sqlite")
        entity = _entity_of(kb)
        assert entity.display_name == "新名"
        # 未覆盖的键由 carryover 保命(upsert 不是整体替换)
        assert entity.aliases == ["旧"]

    async def test_entity_upsert_carries_over_uncovered_keys(self, kb):
        manager = SemanticManager(kb)
        first = await manager.create_draft(
            "demo", "entity", "upsert", "loan",
            payload={"dataset": "loan", "display_name": "贷款",
                     "natural_keys": ["loan_id"], "aliases": ["借款"]})
        await manager.confirm_draft("demo", first["id"], dialect="sqlite")
        second = await manager.create_draft(
            "demo", "entity", "upsert", "loan",
            payload={"dataset": "loan", "aliases": ["放款"]})
        await manager.confirm_draft("demo", second["id"], dialect="sqlite")
        assert _dataset_doc(kb)["entity"] == {
            "display_name": "贷款", "natural_keys": ["loan_id"],
            "aliases": ["放款"]}

    async def test_entity_delete_draft_removes_declaration(self, kb):
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "entity", "upsert", "loan",
            payload={"dataset": "loan", "display_name": "贷款"})
        await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        assert _entity_of(kb) is not None

        removal = await manager.create_draft("demo", "entity", "delete", "loan")
        await manager.confirm_draft("demo", removal["id"], dialect="sqlite")
        assert _entity_of(kb) is None
        assert "entity" not in _dataset_doc(kb)

    def test_entity_delete_on_undeclared_dataset_rejected(self):
        with pytest.raises(ValueError, match="数据集未声明"):
            _apply_draft(_minimal(),
                         {"kind": "entity", "action": "delete", "name": "ghost",
                          "payload": None}, None)

    def test_entity_payload_dataset_mismatch_rejected(self):
        """payload.dataset 与草稿 name 不一致 → 响亮拒绝(静默改归属会让
        「确认的草稿」与「写下的东西」不是同一件事)。"""
        with pytest.raises(ValueError, match="归属数据集不一致"):
            _apply_draft(_minimal({"name": "loan"}), {
                "kind": "entity", "action": "upsert", "name": "loan",
                "payload": {"dataset": "account", "display_name": "贷款"}}, None)

    async def test_entity_on_undeclared_dataset_rejected(self, kb):
        """缺失目标 → 可读的拒绝原因(实体声明只能落在已声明的对象上)。

        create_draft 不校验 payload(校验发生在**确认那一跳**的
        ``_apply_draft``,与全仓「草稿是提货单,合并才是落库」的纪律一致);
        确认响亮失败,草稿保持 pending(可人工修正或以它为蓝本新建)。
        """
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "entity", "upsert", "ghost",
            payload={"dataset": "ghost", "display_name": "幽灵"})
        with pytest.raises(ChangeError, match="数据集未声明"):
            await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        # 拒绝 = 没写:模型语义面仍无痕,草稿仍是 pending
        assert _entity_of(kb) is None
        assert manager.drafts("demo")["pending"][0]["id"] == entry["id"]

    def test_entity_empty_declaration_rejected(self):
        """空声明(entity: {})在解析器里等于未声明 → 确认它没有意义。"""
        for payload in ({}, {"dataset": "loan"},
                        {"dataset": "loan", "display_name": "  "},
                        {"dataset": "loan", "natural_keys": [], "aliases": []}):
            with pytest.raises(ValueError, match="实体声明为空"):
                _apply_draft(_minimal({"name": "loan"}), {
                    "kind": "entity", "action": "upsert", "name": "loan",
                    "payload": payload}, None)

    async def test_entity_dedupes_and_strips_payload_lists(self, kb):
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "entity", "upsert", "loan",
            payload={"dataset": "loan", "natural_keys": ["loan_id", "loan_id", "  "],
                     "aliases": ["借款", "借款"]})
        await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        assert _dataset_doc(kb)["entity"] == {
            "natural_keys": ["loan_id"], "aliases": ["借款"]}

    async def test_pending_entity_draft_is_invisible_to_projection(self, kb):
        """未确认的对象声明对投影不可见:草稿落 pending 前后投影逐字节一致。"""
        manager = SemanticManager(kb)
        before = build_object_graph(manager.model("demo"))
        await manager.create_draft(
            "demo", "entity", "upsert", "loan",
            payload={"dataset": "loan", "display_name": "贷款", "aliases": ["借款"]},
            note="auto:entity_naming:kb-init:loan")
        after = build_object_graph(manager.model("demo"))
        assert after == before
        loan = next(o for o in after["objects"] if o["dataset"] == "loan")
        assert loan["declared"] is False
        assert loan["display_name"] == "loan"  # 读侧回退:数据集名

    def test_validate_draft_gate_for_entity(self, kb):
        """validate 干跑与 confirm 同一条应用路径:坏草稿给可读原因。"""
        doc = SemanticManager(kb).document("demo")
        ok = validate_draft(
            doc, kind="entity", action="upsert", name="loan",
            payload={"dataset": "loan", "display_name": "贷款",
                     "natural_keys": ["loan_id"]})
        assert ok["errors"] == []
        bad = validate_draft(
            doc, kind="entity", action="upsert", name="ghost",
            payload={"dataset": "ghost", "display_name": "幽灵"})
        assert bad["ok"] is False
        assert any("数据集未声明" in m for m in _messages(bad))

    async def test_conflict_annotated_draft_cannot_be_confirmed(self, kb):
        """带冲突注记的草稿照落 pending,但确认被前置守卫拦住(收件箱看得见,
        确认这条路走不通)。"""
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "entity", "upsert", "loan",
            payload={"dataset": "loan", "display_name": "贷款"},
            conflict={"code": "entity_declared",
                      "message": "数据集「loan」已声明实体"})
        pending = manager.drafts("demo")["pending"]
        assert pending[0]["conflict"]["code"] == "entity_declared"
        with pytest.raises(ChangeError, match="冲突"):
            await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        # 守卫拦住 ≠ 写坏:文件面逐字节未动
        assert _entity_of(kb) is None

    async def test_detail_diff_renders_entity_draft(self, kb):
        """管理端 diff 走同一 `_apply_draft`:entity 草稿的 after 面带着
        声明的键(不是「表达式不可解析」)。"""
        manager = SemanticManager(kb)
        await manager.create_draft(
            "demo", "entity", "upsert", "loan",
            payload={"dataset": "loan", "display_name": "贷款"})
        detail = await manager.detail("demo", dialect="sqlite")
        diff = detail["drafts"]["pending"][0]["diff"]
        assert diff["error"] is None
        assert diff["after"]["display_name"] == "贷款"

    async def test_detail_diff_reports_unlandable_entity_draft(self, kb):
        """坏草稿的 diff 带可读原因(收件箱里看得见为什么不能确认)。"""
        manager = SemanticManager(kb)
        await manager.create_draft(
            "demo", "entity", "upsert", "ghost",
            payload={"dataset": "ghost", "display_name": "幽灵"})
        detail = await manager.detail("demo", dialect="sqlite")
        diff = detail["drafts"]["pending"][0]["diff"]
        assert "数据集未声明" in diff["error"]


# ── relationship ─────────────────────────────────────────

_PAIR = {"from": "loan", "to": "account",
         "from_columns": ["account_no"], "to_columns": ["account_no"]}


class TestRelationshipWriteFace:
    async def test_relationship_round_trip(self, kb):
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "relationship", "upsert", "loan_account",
            payload={**_PAIR, "cardinality": "N:1", "display_name": "归属账户"})
        assert manager.model("demo").relationships == []  # 确认前不可见

        await manager.confirm_draft("demo", entry["id"], dialect="sqlite")

        rel = next(r for r in manager.model("demo").relationships
                   if r.name == "loan_account")
        assert (rel.from_, rel.to) == ("loan", "account")
        assert rel.from_columns == ["account_no"]
        assert rel.to_columns == ["account_no"]
        assert rel.cardinality == "N:1"
        assert rel.display_name == "归属账户"
        doc_rel = _semantics_doc(kb)["relationships"][0]
        assert doc_rel["name"] == "loan_account"
        assert doc_rel["from"] == "loan" and doc_rel["to"] == "account"

    async def test_relationship_omits_empty_optional_keys(self, kb):
        """可选键缺席 = 没声明(前端对话框同款契约):留空不写键。"""
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "relationship", "upsert", "r",
            payload={**_PAIR, "cardinality": "N:1", "display_name": "  "})
        await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        doc_rel = _semantics_doc(kb)["relationships"][0]
        assert doc_rel["cardinality"] == "N:1"
        assert "display_name" not in doc_rel
        rel = next(r for r in manager.model("demo").relationships if r.name == "r")
        assert rel.display_name == ""

    def test_relationship_from_to_required(self):
        for payload in ({}, {"from": "loan"}, {"to": "account"},
                        {"from": "  ", "to": "account",
                         "from_columns": ["a"], "to_columns": ["b"]}):
            with pytest.raises(ValueError, match="from/to 必填"):
                _apply_draft(_minimal({"name": "loan"}, {"name": "account"}),
                             {"kind": "relationship", "action": "upsert",
                              "name": "r", "payload": payload}, None)

    def test_relationship_endpoints_must_be_declared(self):
        """关系只能建立在已声明的对象之间 —— 引用不存在的名字产不出边。"""
        with pytest.raises(ValueError, match="引用的数据集未声明"):
            _apply_draft(_minimal({"name": "loan"}),
                         {"kind": "relationship", "action": "upsert",
                          "name": "r",
                          "payload": {"from": "loan", "to": "ghost",
                                      "from_columns": ["a"], "to_columns": ["b"]}},
                         None)

    def test_relationship_column_pairs_required(self):
        """缺键对 → 解析器整体丢弃这条关系(写进去也是不可见的声明)。"""
        base = _minimal({"name": "loan"}, {"name": "account"})
        for payload in (
            {"from": "loan", "to": "account"},  # 两边都没有
            {"from": "loan", "to": "account", "from_columns": ["account_no"]},
            {"from": "loan", "to": "account", "to_columns": ["account_no"]},
            {"from": "loan", "to": "account", "from_columns": ["account_no"],
             "to_columns": []},
        ):
            with pytest.raises(ValueError, match="必填且等长"):
                _apply_draft(base, {"kind": "relationship", "action": "upsert",
                                    "name": "r", "payload": payload}, None)

    def test_relationship_column_pairs_must_be_equal_length(self):
        with pytest.raises(ValueError, match="必填且等长"):
            _apply_draft(
                _minimal({"name": "loan"}, {"name": "account"}),
                {"kind": "relationship", "action": "upsert", "name": "r",
                 "payload": {"from": "loan", "to": "account",
                             "from_columns": ["a", "b"],
                             "to_columns": ["c"]}}, None)

    async def test_relationship_upsert_carries_over_column_pairs(self, tmp_path):
        """已有关系改端点/显示名时省略键对可以:carryover 把旧键对补回来。"""
        seeded = _kb_with(tmp_path, _doc(relationship={
            "name": "r", **_PAIR, "cardinality": "N:1"}))
        manager = SemanticManager(seeded)
        entry = await manager.create_draft(
            "demo", "relationship", "upsert", "r",
            payload={"from": "loan", "to": "account", "display_name": "归属"})
        await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        doc_rel = _semantics_doc(seeded)["relationships"][0]
        assert doc_rel["from_columns"] == ["account_no"]
        assert doc_rel["to_columns"] == ["account_no"]
        assert doc_rel["cardinality"] == "N:1"          # carryover
        assert doc_rel["display_name"] == "归属"        # 本次声明

    def test_relationship_fan_out_is_not_payload_settable(self):
        """fan_out 是编译期安全豁免(只应手写)—— 草稿 payload 里的它被
        丢弃,既有的它被 carryover 保住。方向是 fail-safe 的:豁免只会
        少发,不会多发。"""
        base = _minimal({"name": "loan"}, {"name": "account"})
        rels = base["relationships"] = [
            {"name": "r", **_PAIR, "cardinality": "M:N", "fan_out": "dedup"}]
        _apply_draft(base, {
            "kind": "relationship", "action": "upsert", "name": "r",
            "payload": {**_PAIR, "cardinality": "M:N", "fan_out": "off"}}, None)
        assert rels[0]["fan_out"] == "dedup"  # payload 改不动它

        # 新关系的 payload fan_out 同样不写:它只能手写进 semantics.yml
        _apply_draft(base, {
            "kind": "relationship", "action": "upsert", "name": "r2",
            "payload": {**_PAIR, "cardinality": "M:N", "fan_out": "dedup"}}, None)
        assert "fan_out" not in rels[1]

    async def test_relationship_delete_draft_removes_it(self, kb):
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "relationship", "upsert", "r",
            payload={**_PAIR, "cardinality": "N:1"})
        await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        assert [r.name for r in manager.model("demo").relationships] == ["r"]

        removal = await manager.create_draft("demo", "relationship", "delete", "r")
        await manager.confirm_draft("demo", removal["id"], dialect="sqlite")
        assert manager.model("demo").relationships == []

    async def test_relationship_delete_of_missing_name_is_noop(self, kb):
        manager = SemanticManager(kb)
        removal = await manager.create_draft("demo", "relationship", "delete", "nope")
        await manager.confirm_draft("demo", removal["id"], dialect="sqlite")
        assert manager.model("demo") is not None  # 文件仍可解析

    def test_validate_draft_gate_for_relationship(self, kb):
        doc = SemanticManager(kb).document("demo")
        ok = validate_draft(
            doc, kind="relationship", action="upsert", name="r",
            payload={**_PAIR, "cardinality": "N:1"})
        assert ok["errors"] == []
        bad = validate_draft(
            doc, kind="relationship", action="upsert", name="r",
            payload={"from": "loan", "to": "ghost",
                     "from_columns": ["a"], "to_columns": ["b"]})
        assert bad["ok"] is False
        assert any("未声明" in m for m in _messages(bad))

    async def test_relationship_reaches_projection_as_a_visible_edge(self, kb):
        """端到端:确认后的关系在投影里是一条可见的边(不是静默丢弃的声明)。"""
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "relationship", "upsert", "loan_account",
            payload={**_PAIR, "cardinality": "N:1"})
        await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        graph = build_object_graph(manager.model("demo"))
        link = next(link for link in graph["links"] if link["name"] == "loan_account")
        assert link["from"] == "loan" and link["to"] == "account"
        assert link["from_columns"] == ["account_no"]
