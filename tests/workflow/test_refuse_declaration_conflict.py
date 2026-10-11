"""E2 冲突分流:实体/关系草稿的确定性冲突检测与蓝本预填。

``_detect_conflict`` 的第一条纪律是**分流**:实体/关系草稿没有
``expression`` 键(声明内容在 payload 里,前端对话框的自有形状),不先
分流就会被 ``_expr_ok("")`` 一律判成「表达式不可解析」—— 一个正确的新
关系声明会被标成不可解析,蓝本里还会多出一行空的表达式。

本文件只钉纯函数面(``_detect_declaration_conflict`` /
``_detect_conflict`` / ``_blueprint_payload`` / ``_conflict_phrase``):
refuse 节点的落库时机在节点测试面,这层不重复。
"""

from __future__ import annotations

import yaml

from trove.services.semantic_layer.ossie import parse_ossie
from trove.workflow.nodes.refuse import (
    Conflict,
    _CONFLICT_COPY,
    _blueprint_payload,
    _conflict_phrase,
    _detect_conflict,
)


def _model(*, entity: dict | None = None, relationship: dict | None = None):
    loan: dict = {
        "name": "loan", "source": "loan", "primary_key": ["loan_id"],
        "fields": [
            {"name": "loan_id", "datatype": "Integer",
             "expression": {"dialects": [{
                 "dialect": "ANSI_SQL", "expression": "loan_id"}]}},
            {"name": "account_no", "datatype": "String",
             "expression": {"dialects": [{
                 "dialect": "ANSI_SQL", "expression": "account_no"}]}},
        ],
    }
    if entity is not None:
        loan["entity"] = entity
    account: dict = {
        "name": "account", "source": "account",
        "fields": [
            {"name": "account_no", "datatype": "String",
             "expression": {"dialects": [{
                 "dialect": "ANSI_SQL", "expression": "account_no"}]}},
        ],
    }
    entry: dict = {"name": "demo", "datasets": [loan, account], "metrics": []}
    if relationship is not None:
        entry["relationships"] = [relationship]
    doc = {"version": "0.2.0.dev0", "semantic_model": [entry]}
    return parse_ossie(yaml.safe_dump(doc, allow_unicode=True))


_ENTITY_DRAFT = {"kind": "entity", "action": "upsert", "name": "loan",
                 "payload": {"dataset": "loan", "display_name": "贷款"}}

_REL_DRAFT = {"kind": "relationship", "action": "upsert", "name": "loan_account",
              "payload": {"from": "loan", "to": "account",
                          "from_columns": ["account_no"],
                          "to_columns": ["account_no"]}}


class TestDeclarationConflictDetection:
    def test_entity_is_never_classified_as_unparseable_expression(self):
        """分流在 ``_expr_ok`` 之前:实体草稿没有 expression,但它是**好的**
        新声明 —— 无冲突可写库。"""
        assert _detect_conflict(_model(), _ENTITY_DRAFT) is None

    def test_entity_on_declared_dataset_without_declaration_is_clean(self):
        # 数据集已声明且还没实体声明 → 直接可写
        conflict = _detect_conflict(_model(), _ENTITY_DRAFT)
        assert conflict is None

    def test_entity_on_undeclared_dataset_is_landable(self):
        conflict = _detect_conflict(
            _model(), {**_ENTITY_DRAFT, "name": "ghost",
                       "payload": {"dataset": "ghost", "display_name": "幽灵"}})
        assert conflict is not None
        assert conflict.code == "dataset_undeclared"
        assert conflict.landable is True
        assert "ghost" in conflict.message

    def test_entity_over_existing_declaration_is_landable_conflict(self):
        """覆盖一条已有实体声明是**有意义的修正路径** → 可修正类冲突:
        草稿照落 pending(收件箱看得见),确认被守卫拦住,给蓝本动作。"""
        model = _model(entity={"display_name": "旧名"})
        conflict = _detect_conflict(model, _ENTITY_DRAFT)
        assert conflict is not None
        assert conflict.code == "entity_declared"
        assert conflict.landable is True
        assert conflict.name == "loan"

    def test_entity_without_name_is_not_landable(self):
        for draft in ({"kind": "entity", "action": "upsert", "name": "",
                       "payload": {"display_name": "无主之名"}},
                      {"kind": "entity", "action": "upsert", "name": "",
                       "payload": None}):
            conflict = _detect_conflict(_model(), draft)
            assert conflict is not None
            assert conflict.code == "name_missing"
            assert conflict.landable is False

    def test_fresh_relationship_is_clean(self):
        assert _detect_conflict(_model(), _REL_DRAFT) is None

    def test_relationship_duplicate_name_is_landable(self):
        model = _model(relationship={
            "name": "loan_account", "from": "loan", "to": "account",
            "from_columns": ["account_no"], "to_columns": ["account_no"],
            "cardinality": "N:1"})
        conflict = _detect_conflict(model, _REL_DRAFT)
        assert conflict is not None
        assert conflict.code == "name_declared"
        assert conflict.landable is True

    def test_relationship_undeclared_endpoint_is_landable(self):
        conflict = _detect_conflict(
            _model(), {**_REL_DRAFT,
                       "payload": {**_REL_DRAFT["payload"], "to": "ghost"}})
        assert conflict is not None
        assert conflict.code == "dataset_undeclared"
        assert conflict.landable is True
        assert "ghost" in conflict.message

    def test_relationship_missing_name_is_not_landable(self):
        conflict = _detect_conflict(_model(), {**_REL_DRAFT, "name": ""})
        assert conflict is not None
        assert conflict.code == "name_missing"
        assert conflict.landable is False

    def test_relationship_missing_endpoints_invents_no_conflict_code(self):
        """from/to 缺失是「草稿结构不全」,不是「与现有模型冲突」——
        这里不发明冲突码(闭集不临时加词),交给确认时的 ``_apply_relationship``
        响亮拒绝。"""
        for payload in (None, {}, {"from": "loan"}, {"to": "account"}):
            assert _detect_conflict(
                _model(), {**_REL_DRAFT, "payload": payload}) is None

    def test_entity_conflict_copy_is_bilingual(self):
        zh, en = _CONFLICT_COPY["entity_declared"]
        assert "实体" in zh and "entity" in en
        for code, pair in _CONFLICT_COPY.items():
            assert len(pair) == 2 and all(str(x).strip() for x in pair), code

    def test_conflict_phrase_uses_code_map_and_falls_back(self):
        conflict = Conflict("entity_declared", "数据集「loan」已声明实体")
        assert _conflict_phrase("zh", conflict) == _CONFLICT_COPY["entity_declared"][0]
        assert _conflict_phrase("en", conflict) == _CONFLICT_COPY["entity_declared"][1]
        # 未知 code 退回泛化说法,绝不漏 message 原文进用户文案
        unknown = Conflict("brand_new_code", "内部诊断原文")
        assert _conflict_phrase("zh", unknown) == "与现有模型冲突"
        assert _conflict_phrase("en", unknown) == "it conflicts with the current model"


class TestBlueprintPayload:
    def test_relationship_blueprint_carries_frontend_prefill_keys(self):
        """关系蓝本的键名 = 前端对话框/NextActions 的 ``bp_*`` 契约。"""
        out = _blueprint_payload(
            _REL_DRAFT,
            Conflict("name_declared", "关系「loan_account」已声明", landable=True))
        assert out["draft_kind"] == "relationship"
        assert out["draft_name"] == "loan_account"
        assert out["from_dataset"] == "loan"
        assert out["to_dataset"] == "account"
        assert out["from_columns"] == ["account_no"]
        assert out["to_columns"] == ["account_no"]
        assert out["conflict_code"] == "name_declared"
        assert out["conflict_message"] == "关系「loan_account」已声明"

    def test_entity_blueprint_shape_unchanged(self):
        """实体蓝本不新增键:前端预填面(E7 之前)没有实体分支,多给键
        等于给出一个没有消费方的契约。"""
        out = _blueprint_payload(
            _ENTITY_DRAFT,
            Conflict("entity_declared", "数据集「loan」已声明实体", landable=True))
        assert set(out) == {"draft_kind", "draft_name", "expression",
                            "conflict_code", "conflict_message"}
        assert out["draft_kind"] == "entity"
        assert out["draft_name"] == "loan"

    def test_metric_blueprint_key_set_pinned(self):
        """老形状钉死:metric 蓝本键集与既有一致(新分支不得顺手动它)。"""
        draft = {"kind": "metric", "name": "refund_rate",
                 "expression": "SUM(loan.refund) / SUM(loan.amount)",
                 "datasets": ["loan"]}
        out = _blueprint_payload(draft, Conflict(
            "dataset_undeclared", "引用了未声明的数据集 ['loan']", landable=True))
        assert set(out) == {"draft_kind", "draft_name", "expression", "datasets",
                            "conflict_code", "conflict_message"}
        assert out["datasets"] == ["loan"]
        assert out["conflict_code"] == "dataset_undeclared"
