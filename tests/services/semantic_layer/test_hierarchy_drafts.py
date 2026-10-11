"""E7 §manage.py:钻取层级(hierarchy)声明的草稿写面。

与 ``test_entity_relationship_drafts.py`` 同一条链(E2 建链、E7 加 kind):
``create_draft`` 只写 ``semantic_drafts.yml``(pending),``confirm_draft``
经 ``ChangeService`` 合并进 ``semantics.yml``(单一真源)。本文件钉:

1. **kind 收发**:``hierarchy`` 进闭集、走 ``_apply_hierarchy`` 分发,
   未知 kind 照旧当场拒绝;
2. **写盘前硬校验**:归属数据集必须已声明(级别字段无处解析的层级没有
   消费面)、``levels`` 至少一个(空链看起来"确认成功"却钻不下去);
3. **往返**:create → confirm → YAML → ``parse_ossie`` 模型侧;
4. **carryover**:upsert 不抹未覆盖的 dataset/levels/display_name;
5. **未确认不可见 + 拒绝 = 没写**:pending 草稿不改变模型;确认失败草稿
   仍 pending;
6. **删除**:delete 草稿移除声明;删不存在的名字是 no-op。

harness 直接复用 E2 文件的 ``_doc``/``_kb_with``(同一份最小模型:loan
带 loan_id/amount/account_no,account 带 account_no/balance)。
"""

from __future__ import annotations

import pytest

from tests.services.semantic_layer.test_entity_relationship_drafts import (
    _doc,
    _kb_with,
    _minimal,
    _semantics_doc,
)
from trove.services.semantic_layer.changes import ChangeError
from trove.services.semantic_layer.issues import validate_draft
from trove.services.semantic_layer.manage import (
    _KINDS,
    SemanticManager,
    _apply_draft,
)


@pytest.fixture
def kb(tmp_path):
    return _kb_with(tmp_path, _doc())


def _hierarchies(kb) -> list:
    return SemanticManager(kb).model("demo").hierarchies


def _hierarchy_doc(kb) -> dict:
    return _semantics_doc(kb)["semantic_model"][0]


_PAYLOAD = {"dataset": "loan", "display_name": "地理",
            "levels": ["account_no", "loan_id"]}


class TestHierarchyKindWiring:
    def test_hierarchy_is_in_the_kind_closure(self):
        assert "hierarchy" in _KINDS

    def test_unknown_kind_still_rejected(self):
        with pytest.raises(ValueError, match="未知草稿类型"):
            _apply_draft(_minimal({"name": "loan"}), {
                "kind": "widget", "action": "upsert", "name": "x",
                "payload": {}}, None)

    def test_upsert_lands_in_the_model_hierarchies_block(self):
        model = _minimal({"name": "loan"})
        _apply_draft(model, {"kind": "hierarchy", "action": "upsert",
                             "name": "geo", "payload": _PAYLOAD}, None)
        assert model["semantic_model"][0]["hierarchies"] == [
            {"name": "geo", "dataset": "loan", "display_name": "地理",
             "levels": ["account_no", "loan_id"]}]

    async def test_duplicate_levels_deduped_and_blanks_dropped(self, kb):
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "hierarchy", "upsert", "geo",
            payload={"dataset": "loan",
                     "levels": ["account_no", "account_no", "  ", "loan_id"]})
        await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        assert _hierarchy_doc(kb)["hierarchies"][0]["levels"] == [
            "account_no", "loan_id"]


class TestHierarchyWriteValidation:
    def test_dataset_must_be_declared(self):
        with pytest.raises(ValueError, match="未声明"):
            _apply_draft(_minimal({"name": "loan"}), {
                "kind": "hierarchy", "action": "upsert", "name": "geo",
                "payload": {"dataset": "nope", "levels": ["loan_id"]}}, None)

    def test_missing_dataset_rejected(self):
        with pytest.raises(ValueError, match="dataset 必填"):
            _apply_draft(_minimal({"name": "loan"}), {
                "kind": "hierarchy", "action": "upsert", "name": "geo",
                "payload": {"levels": ["loan_id"]}}, None)

    def test_empty_levels_rejected(self):
        with pytest.raises(ValueError, match="levels 必填"):
            _apply_draft(_minimal({"name": "loan"}), {
                "kind": "hierarchy", "action": "upsert", "name": "geo",
                "payload": {"dataset": "loan", "levels": []}}, None)

    async def test_confirm_failure_keeps_the_draft_pending(self, kb):
        """拒绝 = 没写:校验发生在确认那一跳,失败后模型无痕、草稿仍 pending。"""
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "hierarchy", "upsert", "geo",
            payload={"dataset": "nope", "display_name": "地理",
                     "levels": ["loan_id"]})
        with pytest.raises(ChangeError, match="数据集.*未声明"):
            await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        assert _hierarchies(kb) == []
        assert manager.drafts("demo")["pending"][0]["id"] == entry["id"]

    def test_validate_draft_gate_reports_undeclared_level_field(self, kb):
        """级别字段必须已声明 —— 悬空级别是 lint 硬错,干跑给可读原因。"""
        doc = SemanticManager(kb).document("demo")
        ok = validate_draft(doc, kind="hierarchy", action="upsert", name="geo",
                            payload=dict(_PAYLOAD))
        assert ok["errors"] == []
        bad = validate_draft(doc, kind="hierarchy", action="upsert", name="geo",
                             payload={"dataset": "loan",
                                      "levels": ["loan_id", "ghost"]})
        assert bad["ok"] is False
        assert any("级别字段" in e["message"] for e in bad["errors"])


class TestHierarchyRoundTrip:
    async def test_create_confirm_yaml_model(self, kb):
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "hierarchy", "upsert", "geo", payload=dict(_PAYLOAD),
            note="q")
        # 未确认 = 未声明:pending 草稿不改变模型
        assert manager.drafts("demo")["pending"][0]["id"] == entry["id"]
        assert _hierarchies(kb) == []

        await manager.confirm_draft("demo", entry["id"], dialect="sqlite")

        h = _hierarchies(kb)
        assert len(h) == 1
        assert h[0].name == "geo" and h[0].display_name == "地理"
        assert h[0].dataset == "loan"
        assert h[0].levels == ["account_no", "loan_id"]
        # YAML 形状:只含声明了的键
        assert _hierarchy_doc(kb)["hierarchies"] == [
            {"name": "geo", "dataset": "loan", "display_name": "地理",
             "levels": ["account_no", "loan_id"]}]
        assert manager.drafts("demo")["pending"] == []
        assert manager.drafts("demo")["applied"][0]["status"] == "applied"

    async def test_upsert_carries_over_uncovered_keys(self, kb):
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "hierarchy", "upsert", "geo", payload=dict(_PAYLOAD))
        await manager.confirm_draft("demo", entry["id"], dialect="sqlite")

        # 二次 upsert 只改展示名:dataset/levels 不被抹掉
        second = await manager.create_draft(
            "demo", "hierarchy", "upsert", "geo",
            payload={"display_name": "地区"})
        await manager.confirm_draft("demo", second["id"], dialect="sqlite")
        assert _hierarchy_doc(kb)["hierarchies"] == [
            {"name": "geo", "dataset": "loan", "display_name": "地区",
             "levels": ["account_no", "loan_id"]}]

    async def test_absent_hierarchies_untouched_by_other_kinds(self, kb):
        """未声明层级全部路径不变:其它 kind 的确认不产生 hierarchies 键。"""
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "entity", "upsert", "loan",
            payload={"dataset": "loan", "display_name": "贷款"})
        await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        assert _hierarchy_doc(kb).get("hierarchies") in (None, [])
        assert _hierarchies(kb) == []

    async def test_delete_draft_removes_the_declaration(self, kb):
        manager = SemanticManager(kb)
        entry = await manager.create_draft(
            "demo", "hierarchy", "upsert", "geo", payload=dict(_PAYLOAD))
        await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        assert _hierarchies(kb)

        removal = await manager.create_draft("demo", "hierarchy", "delete", "geo")
        await manager.confirm_draft("demo", removal["id"], dialect="sqlite")
        assert _hierarchies(kb) == []
        assert _hierarchy_doc(kb).get("hierarchies") in (None, [])

    def test_delete_of_missing_name_is_noop(self):
        _apply_draft(_minimal({"name": "loan"}), {
            "kind": "hierarchy", "action": "delete", "name": "ghost",
            "payload": None}, None)
