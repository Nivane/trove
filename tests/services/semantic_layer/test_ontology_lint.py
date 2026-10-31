"""本体形态 E3:三块声明的 lint 与门禁分级(设计 §4.1/§4.3,拍板 Q4)。

覆盖四层:

1. ``_lint_entities`` / ``_lint_hierarchies`` 的逐规则正反例 —— 结构性
   error(声明本身不成立)与语义缺口 warning(提示不阻断)分开断言;
2. ``lint_semantics_document`` 的放错层硬错(与 masking/topics 同一条
   双守卫先例:N3);
3. ``issues.py`` 的翻译与分级 —— 新 kind(entity/hierarchy)的结构化映射、
   ``blocking_issues`` 判据(error 拦、豁免 warning 放行、既有 warning 与
   未归类串照旧拦,fail-closed);
4. 门禁真实路径 —— 写盘前门禁(confirm/auto_apply)与 git pre-commit
   门禁(``KbService.semantics_lint``)同一条判据;validate 干跑一致。

兼容安全带(§4):三块声明缺席 → 全部路径零 issue;单级层级(存量样例
里真实存在)只是 warning,绝不拦写盘。
"""
from __future__ import annotations

import inspect

import pytest
import yaml

from trove.api import schemas
from trove.services.kb.lint import lint_semantics_document
from trove.services.kb.service import KbService
from trove.services.semantic_layer.issues import (
    _ADVISORY_CODES,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    blocking_issues,
    structured_issues,
    validate_draft,
)
from trove.services.semantic_layer.manage import SemanticManager


def _f(name: str) -> dict:
    """最小字段声明(表达式解析得过)。"""
    return {"name": name,
            "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": name}]}}


def _base_doc(*, loan_extra: dict | None = None,
              model_extra: dict | None = None) -> dict:
    """两个数据集 + 空指标的最小文档;新声明按需挂在上面。"""
    loan: dict = {"name": "loan", "source": "loan", "primary_key": ["loan_id"],
                  "fields": [_f("loan_id"), _f("amount")]}
    loan.update(loan_extra or {})
    model: dict = {
        "name": "demo",
        "datasets": [
            loan,
            {"name": "customer", "source": "customer",
             "primary_key": ["customer_id"], "fields": [_f("customer_id")]},
        ],
        "metrics": [],
    }
    model.update(model_extra or {})
    return {"version": "0.2.0.dev0", **model}


def _messages(doc: dict) -> list[str]:
    return lint_semantics_document(doc, dialect="sqlite")


def _hits(doc: dict, needle: str) -> list[str]:
    return [i for i in _messages(doc) if needle in i]


# ── 实体声明(_lint_entities)────────────────────────────────


class TestLintEntities:
    def test_absent_entity_block_is_silent(self):
        """不启用对象视图是合法状态:零 issue(存量模型逐字节同前)。"""
        assert _messages(_base_doc()) == []

    def test_wellformed_entity_passes(self):
        doc = _base_doc(loan_extra={"entity": {
            "display_name": "贷款", "natural_keys": ["loan_id"],
            "aliases": ["借款"]}})
        assert _messages(doc) == []

    def test_natural_key_must_be_declared_field(self):
        doc = _base_doc(loan_extra={"entity": {"natural_keys": ["ghost"]}})
        hits = _hits(doc, "实体自然键「ghost」")
        assert len(hits) == 1 and "已声明字段" in hits[0]

    def test_declared_natural_key_is_clean(self):
        doc = _base_doc(loan_extra={"entity": {"natural_keys": ["amount"]}})
        assert _hits(doc, "实体自然键") == []

    def test_natural_keys_bad_shape_is_error(self):
        doc = _base_doc(loan_extra={"entity": {"natural_keys": {"a": 1}}})
        assert _hits(doc, "natural_keys 必须是名字数组")

    def test_entity_block_bad_shape_is_error(self):
        """坏形状会被装载侧整体丢弃 —— 声明了却不在模型里,拦下。"""
        doc = _base_doc(loan_extra={"entity": ["loan_id"]})
        assert _hits(doc, "entity 声明必须是映射")

    def test_entity_without_keys_falls_back_to_primary_key(self):
        """natural_keys 缺省回退 primary_key(读侧纪律):有回退就不提示。"""
        doc = _base_doc(loan_extra={"entity": {"display_name": "贷款"}})
        assert _hits(doc, "未声明自然键") == []

    def test_entity_without_keys_and_primary_key_warns(self):
        """回退链两头都空 → 语义缺口 warning(提示但不阻断,Q4)。"""
        doc = _base_doc(loan_extra={
            "primary_key": [], "entity": {"display_name": "贷款"}})
        hits = _hits(doc, "声明了实体但未声明自然键")
        assert len(hits) == 1 and "primary_key" in hits[0]

    def test_empty_entity_block_equals_absent(self):
        """``entity: {}`` 与缺席同义(装载侧归一为 None):零 issue。"""
        assert _messages(_base_doc(loan_extra={"entity": {}})) == []

    def test_alias_duplicating_own_synonym_warns(self):
        doc = _base_doc(loan_extra={
            "ai_context": {"synonyms": ["借款"]},
            "entity": {"aliases": ["借款"]}})
        assert _hits(doc, "与自身 synonyms 重复")

    def test_alias_conflicting_with_other_dataset_synonym_warns(self):
        doc = _base_doc(loan_extra={"entity": {"aliases": ["分期"]}})
        doc["datasets"][1]["ai_context"] = {
            "synonyms": ["分期"]}
        hits = _hits(doc, "实体别名「分期」")
        assert len(hits) == 1 and "customer" in hits[0]

    def test_alias_conflicting_with_other_entity_alias_warns(self):
        """别名跨实体重复:同一对只报一次(报后来者),判定确定。"""
        doc = _base_doc(loan_extra={"entity": {"aliases": ["账户"]}})
        doc["datasets"][1]["entity"] = {
            "aliases": ["账户"]}
        hits = _hits(doc, "实体别名「账户」")
        assert len(hits) == 1 and "customer" in hits[0]

    def test_distinct_aliases_do_not_warn(self):
        doc = _base_doc(loan_extra={"entity": {"aliases": ["借款"]}})
        doc["datasets"][1]["entity"] = {
            "aliases": ["客户"]}
        assert _hits(doc, "实体别名") == []

    def test_alias_bad_shape_is_error(self):
        doc = _base_doc(loan_extra={"entity": {"aliases": {"a": 1}}})
        assert _hits(doc, "aliases 必须是名字数组")

    def test_scalar_aliases_tolerated(self):
        """裸标量 aliases 是**一个**别名(装载侧同法),不是坏形状。"""
        doc = _base_doc(loan_extra={"entity": {"aliases": "借款"}})
        assert _hits(doc, "aliases 必须是名字数组") == []


# ── 钻取层级(_lint_hierarchies)─────────────────────────────


class TestLintHierarchies:
    def test_absent_hierarchies_is_silent(self):
        assert _messages(_base_doc(model_extra={"hierarchies": []})) == []

    def test_wellformed_hierarchy_passes(self):
        doc = _base_doc(model_extra={"hierarchies": [
            {"name": "geo", "dataset": "loan", "levels": ["loan_id", "amount"]}]})
        assert _messages(doc) == []

    def test_hierarchies_not_a_list_is_error(self):
        doc = _base_doc(model_extra={"hierarchies": {"name": "geo"}})
        assert _hits(doc, "hierarchies 必须是数组")

    def test_hierarchy_entry_not_a_mapping_is_error(self):
        doc = _base_doc(model_extra={"hierarchies": ["geo"]})
        assert _hits(doc, "hierarchies 条目必须是映射")

    def test_hierarchy_missing_name_is_error(self):
        doc = _base_doc(model_extra={"hierarchies": [
            {"dataset": "loan", "levels": ["loan_id", "amount"]}]})
        assert _hits(doc, "钻取层级缺少 name")

    def test_duplicate_hierarchy_name_is_error(self):
        """层级靠名字被引用,重名让引用有歧义。"""
        entry = {"name": "geo", "dataset": "loan",
                 "levels": ["loan_id", "amount"]}
        doc = _base_doc(model_extra={"hierarchies": [entry, dict(entry)]})
        assert _hits(doc, "层级「geo」重复定义")

    def test_hierarchy_without_dataset_is_error(self):
        doc = _base_doc(model_extra={"hierarchies": [
            {"name": "geo", "levels": ["loan_id", "amount"]}]})
        assert _hits(doc, "未声明归属数据集 dataset")

    def test_hierarchy_dataset_must_be_declared(self):
        doc = _base_doc(model_extra={"hierarchies": [
            {"name": "geo", "dataset": "nope",
             "levels": ["loan_id", "amount"]}]})
        assert _hits(doc, "声明的数据集「nope」不存在")
        clean = _base_doc(model_extra={"hierarchies": [
            {"name": "geo", "dataset": "loan",
             "levels": ["loan_id", "amount"]}]})
        assert _hits(clean, "声明的数据集") == []

    def test_hierarchy_level_must_be_declared_field(self):
        doc = _base_doc(model_extra={"hierarchies": [
            {"name": "geo", "dataset": "loan",
             "levels": ["loan_id", "ghost"]}]})
        hits = _hits(doc, "级别字段「ghost」")
        assert len(hits) == 1 and "不在数据集「loan」的 fields 中" in hits[0]

    def test_single_level_hierarchy_warns_not_errors(self):
        """单级链是语义缺口(提示),不是结构错位 —— 存量样例真实存在。"""
        doc = _base_doc(model_extra={"hierarchies": [
            {"name": "geo", "dataset": "loan", "levels": ["loan_id"]}]})
        hits = _hits(doc, "解析后只有 1 个级别")
        assert len(hits) == 1
        item = structured_issues(hits)[0]
        assert item["severity"] == SEVERITY_WARNING
        assert item["code"] == "hierarchy_levels_sparse"

    def test_duplicate_level_warns_without_double_reporting(self):
        """[a, a] 只报重复(修掉它就是两级链),不再叠报"单级链条"。"""
        doc = _base_doc(model_extra={"hierarchies": [
            {"name": "geo", "dataset": "loan",
             "levels": ["loan_id", "loan_id"]}]})
        assert _hits(doc, "级别「loan_id」重复")
        assert _hits(doc, "解析后只有") == []

    def test_hierarchy_levels_bad_shape_is_error(self):
        doc = _base_doc(model_extra={"hierarchies": [
            {"name": "geo", "dataset": "loan", "levels": {"a": 1}}]})
        assert _hits(doc, "levels 必须是名字数组")

    def test_scalar_levels_is_tolerated_as_single_level(self):
        """裸标量 levels 是一个级别(装载侧同法):只提示单级链。"""
        doc = _base_doc(model_extra={"hierarchies": [
            {"name": "geo", "dataset": "loan", "levels": "loan_id"}]})
        assert _hits(doc, "levels 必须是名字数组") == []
        assert _hits(doc, "解析后只有 1 个级别")


# ── 口径覆盖(FR-HIER-3,_lint_metric_hierarchy_coverage)──────


def _cov_doc(*, metric_filter: str = "", metric_expr: str = "SUM(amount)",
             levels: tuple[str, ...] = ("region", "loan_id")) -> dict:
    """层级 + 单指标的最小文档(region 维度、ts 时态,均声明字段)。"""
    fields = [
        _f("loan_id"), _f("amount"),
        {"name": "region", "expression": {"dialects": [
            {"dialect": "ANSI_SQL", "expression": "region"}]},
         "semantic_role": "dimension"},
        {"name": "ts", "expression": {"dialects": [
            {"dialect": "ANSI_SQL", "expression": "ts"}]},
         "semantic_role": "time"},
    ]
    metric: dict = {"name": "loan_balance", "expression": {"dialects": [
        {"dialect": "ANSI_SQL", "expression": metric_expr}]}}
    if metric_filter:
        metric["filter"] = metric_filter
    return _base_doc(
        loan_extra={"fields": fields},
        model_extra={
            "metrics": [metric],
            "hierarchies": [{"name": "geo", "dataset": "loan",
                             "levels": list(levels)}],
        })


class TestHierarchyCoverageGap:
    """指标口径把层级收窄到某一支 → warning(不阻断)且可结构化消费。"""

    NEEDLE = "过滤/派生条件对层级"

    def test_equality_on_a_level_field_warns(self):
        hits = _hits(_cov_doc(metric_filter="region = '个人'"), self.NEEDLE)
        assert len(hits) == 1
        assert "指标「loan_balance」" in hits[0]
        assert "层级「geo」" in hits[0] and "「region」" in hits[0]

    @pytest.mark.parametrize("pred", [
        "region != '个人'",           # 不等同款收敛
        "region IN ('个人', '小微')",  # IN(含 NOT IN)
    ])
    def test_neq_and_in_also_warn(self, pred):
        assert _hits(_cov_doc(metric_filter=pred), self.NEEDLE)

    def test_expression_pin_is_reported_too(self):
        doc = _cov_doc(metric_expr=(
            "SUM(CASE WHEN region = '个人' THEN amount ELSE 0 END)"))
        assert _hits(doc, self.NEEDLE)

    def test_ordering_comparison_is_not_reported(self):
        """顺序比较不"收敛"到具名支 —— 时间窗口类惯用写法落这一侧。"""
        for pred in ("region >= '个'", "region < '微'"):
            assert _hits(_cov_doc(metric_filter=pred), self.NEEDLE) == []

    def test_temporal_level_field_is_not_reported(self):
        """时间等值是「时间锚」不是「层级分支」(N35 边界,不误报)。"""
        doc = _cov_doc(metric_filter="ts = '2026-01-01'",
                       levels=("ts", "region"))
        assert _hits(doc, self.NEEDLE) == []

    def test_undeclared_column_is_silent(self):
        assert _hits(_cov_doc(metric_filter="city = 'x'"), self.NEEDLE) == []

    def test_absent_hierarchies_is_silent(self):
        doc = _cov_doc(metric_filter="region = '个人'")
        doc["hierarchies"] = []
        assert _hits(doc, self.NEEDLE) == []

    def test_gap_maps_to_metric_target_advisory_and_does_not_block(self):
        doc = _cov_doc(metric_filter="region = '个人'")
        issues = _messages(doc)
        assert issues and all(self.NEEDLE in i for i in issues)
        item = structured_issues(issues)[0]
        assert item["code"] == "hierarchy_coverage_gap"
        assert item["severity"] == SEVERITY_WARNING
        assert item["target"] == {"kind": "metric", "name": "loan_balance"}
        assert blocking_issues(issues) == []   # 语义缺口提示,放行


# ── 放错层(lint_semantics_document 硬错)────────────────────


class TestDocumentPlacement:
    def test_top_level_entity_is_hard_error(self):
        doc = _base_doc()
        doc["entity"] = {"display_name": "贷款"}
        hits = _hits(doc, "文档根上的 entity 会被忽略")
        assert len(hits) == 1 and "datasets[].entity" in hits[0]

    def test_top_level_hierarchies_are_valid(self):
        """0.2 扁平根:``hierarchies`` 与 datasets/metrics 同在文档根。"""
        doc = _base_doc(model_extra={"hierarchies": [
            {"name": "geo", "dataset": "loan",
             "levels": ["loan_id", "amount"]}]})
        assert _messages(doc) == []

    def test_correctly_placed_declarations_are_not_misplaced(self):
        doc = _base_doc(
            loan_extra={"entity": {"display_name": "贷款",
                                   "natural_keys": ["loan_id"]}},
            model_extra={"hierarchies": [
                {"name": "geo", "dataset": "loan",
                 "levels": ["loan_id", "amount"]}]})
        assert _hits(doc, "文档根上的") == []


# ── 结构化映射(kind 词汇表 N13)────────────────────────────


class TestStructuredIssuesNewKinds:
    def test_entity_issues_map_to_entity_target(self):
        doc = _base_doc(loan_extra={"entity": {"natural_keys": ["ghost"]}})
        item = structured_issues(_hits(doc, "实体自然键"))[0]
        assert item["code"] == "entity_natural_key"
        assert item["severity"] == SEVERITY_ERROR
        assert item["target"] == {"kind": "entity", "name": "loan"}
        assert item["hint"] and item["hint"] != structured_issues(
            ["半句没见过的问题描述"])[0]["hint"]

    def test_advisory_entity_issue_maps_to_warning(self):
        doc = _base_doc(loan_extra={
            "primary_key": [], "entity": {"display_name": "贷款"}})
        item = structured_issues(_hits(doc, "未声明自然键"))[0]
        assert item["code"] == "entity_natural_key_missing"
        assert item["severity"] == SEVERITY_WARNING
        assert item["target"] == {"kind": "entity", "name": "loan"}

    def test_alias_conflict_maps_to_entity_target(self):
        doc = _base_doc(loan_extra={"entity": {"aliases": ["分期"]}})
        doc["datasets"][1]["ai_context"] = {
            "synonyms": ["分期"]}
        item = structured_issues(_hits(doc, "实体别名"))[0]
        assert item["code"] == "entity_alias_conflict"
        assert item["severity"] == SEVERITY_WARNING
        assert item["target"] == {"kind": "entity", "name": "loan"}

    def test_hierarchy_issues_map_to_hierarchy_target(self):
        doc = _base_doc(model_extra={"hierarchies": [
            {"name": "geo", "dataset": "loan",
             "levels": ["loan_id", "ghost"]}]})
        item = structured_issues(_hits(doc, "级别字段"))[0]
        assert item["code"] == "hierarchy_level_invalid"
        assert item["severity"] == SEVERITY_ERROR
        assert item["target"] == {"kind": "hierarchy", "name": "geo"}

    def test_format_issues_map_to_document_target(self):
        item = structured_issues(_messages(
            {"semantic_model": [{"name": "demo"}]}))[0]
        assert item["code"] == "ossie_format"
        assert item["severity"] == SEVERITY_ERROR
        assert item["target"] == {"kind": "document", "name": ""}

    def test_issue_kind_vocabulary_is_declared_in_api_schema(self):
        """N13:结构化输出的每个 kind 都在 API schema 的闭集注释里登记。"""
        samples = [
            "指标「m」重复定义",                       # metric
            "表 loan.amount 表达式无法解析: x",        # field
            "表 loan unique_keys 引用未声明的列 x",    # dataset
            "关系「r」未声明基数(cardinality),编译器将保守 MISS",  # relationship
            "主题域「t」重复定义",                     # topic
            "数据集「loan」的实体自然键「ghost」不是本数据集已声明字段(x)",  # entity
            "层级「geo」重复定义",                     # hierarchy
            "文档根上的 entity 会被忽略 —— 请写在 datasets[].entity",  # document
            "半句没见过的问题描述",                    # unknown
        ]
        kinds = {i["target"]["kind"] for i in structured_issues(samples)}
        text = inspect.getsource(schemas.SemanticIssueTarget)
        for kind in kinds:
            assert kind in text, f"kind {kind!r} 未在 API schema 词汇表里登记"


# ── blocking_issues 分级(唯一判据)─────────────────────────


class TestBlockingIssuesGrading:
    def test_advisory_codes_are_exactly_the_declared_gaps(self):
        """豁免名单只收新声明的语义缺口 —— 增改必须是有意的。"""
        assert _ADVISORY_CODES == {
            "entity_natural_key_missing",
            "entity_alias_conflict",
            "hierarchy_levels_sparse",
            "hierarchy_level_dup",
            # E7 FR-HIER-3:指标口径收敛到层级某支是语义缺口提示,不阻断
            "hierarchy_coverage_gap",
        }

    def test_structural_errors_block(self):
        doc = _base_doc(
            loan_extra={"entity": {"natural_keys": ["ghost"]}},
            model_extra={"hierarchies": [
                {"name": "geo", "dataset": "nope",
                 "levels": ["a", "b"]}]})
        assert blocking_issues(_messages(doc)) == _messages(doc)

    @pytest.mark.parametrize("kind,extra", [
        ("entity", {"primary_key": [], "entity": {"display_name": "贷款"}}),
        ("alias", {"entity": {"aliases": ["分期"]}}),
        ("single_level", None),
        ("dup_level", None),
    ])
    def test_advisory_warnings_do_not_block(self, kind, extra):
        model_extra = None
        if kind == "single_level":
            model_extra = {"hierarchies": [
                {"name": "geo", "dataset": "loan", "levels": ["loan_id"]}]}
        if kind == "dup_level":
            model_extra = {"hierarchies": [
                {"name": "geo", "dataset": "loan",
                 "levels": ["loan_id", "loan_id"]}]}
        doc = _base_doc(loan_extra=extra, model_extra=model_extra)
        if kind == "alias":
            doc["datasets"][1]["ai_context"] = {
                "synonyms": ["分期"]}
        issues = _messages(doc)
        assert issues  # 提示得到……
        assert blocking_issues(issues) == []  # ……但不阻断

    def test_legacy_warning_still_blocks(self):
        """兼容安全带:既有 warning 的判据不因分级而放宽。"""
        doc = _base_doc()
        doc["datasets"][0]["unique_keys"] = [["ghost"]]
        issues = _messages(doc)
        assert any("unique_keys" in i for i in issues)
        assert blocking_issues(issues) == issues

    def test_unclassified_string_still_blocks(self):
        """fail-closed:未命中规则表的串照旧拦 —— 忘了登记不会悄悄放行。"""
        assert blocking_issues(["半句没见过的问题描述"]) == [
            "半句没见过的问题描述"]

    def test_blocking_preserves_order(self):
        issues = [
            "层级「geo」重复定义",
            "层级「geo」解析后只有 1 个级别 —— 单级链条没有可钻的下一级(x)",
            "指标「m」重复定义",
        ]
        assert blocking_issues(issues) == [issues[0], issues[2]]


# ── 写盘/提交门禁的真实路径 ────────────────────────────────


def _seed_kb(tmp_path, text: str) -> KbService:
    kb = KbService(tmp_path / "proj")
    (kb.kb_dir / "demo").mkdir(parents=True, exist_ok=True)
    kb.semantics_path("demo").write_text(text, encoding="utf-8")
    return kb


#: 仅含语义缺口(实体自然键回退缺失)的文档 —— 唯一 issue 是可豁免 warning。
_ADVISORY_YAML = """version: "0.2.0.dev0"
name: demo
datasets:
- name: loan
  source: loan
  entity: {display_name: 贷款}
  fields:
  - name: loan_id
    expression: {dialects: [{dialect: ANSI_SQL, expression: loan_id}]}
metrics: []
"""

#: 结构性错位(自然键引用未声明字段)—— 必须拦下。
_STRUCTURAL_YAML = """version: "0.2.0.dev0"
name: demo
datasets:
- name: loan
  source: loan
  primary_key: [loan_id]
  entity: {natural_keys: [ghost]}
  fields:
  - name: loan_id
    expression: {dialects: [{dialect: ANSI_SQL, expression: loan_id}]}
metrics: []
"""


class TestWriteGateIntegration:
    async def test_structural_entity_error_blocks_confirm(self, tmp_path):
        """error 拦写盘:不落盘、草稿仍 pending。"""
        kb = _seed_kb(tmp_path, _STRUCTURAL_YAML)
        before = kb.semantics_path("demo").read_text(encoding="utf-8")
        manager = SemanticManager(kb)
        draft = await manager.create_draft(
            "demo", "metric", "upsert", "loan_count",
            {"expression": "COUNT(loan.loan_id)"})

        with pytest.raises(ValueError) as ei:
            await manager.confirm_draft("demo", draft["id"], dialect="sqlite")
        assert "拒绝写入" in str(ei.value)
        assert "实体自然键「ghost」" in str(ei.value)

        assert kb.semantics_path("demo").read_text(encoding="utf-8") == before
        assert manager.drafts("demo")["pending"][0]["id"] == draft["id"]

    async def test_advisory_entity_warning_does_not_block_confirm(self, tmp_path):
        """warning 不阻断:写盘成功,且问题照常出现在管理端 issues 里。"""
        kb = _seed_kb(tmp_path, _ADVISORY_YAML)
        manager = SemanticManager(kb)
        # 提示得到:管理端列表里就有这条语义缺口
        assert any("未声明自然键" in i for i in manager.issues("demo"))
        draft = await manager.create_draft(
            "demo", "metric", "upsert", "loan_count",
            {"expression": "COUNT(loan.loan_id)"})

        await manager.confirm_draft("demo", draft["id"], dialect="sqlite")

        assert "loan_count" in kb.semantics_path("demo").read_text(encoding="utf-8")

    async def test_single_level_hierarchy_does_not_block_confirm(self, tmp_path):
        """存量样例兼容:单级层级只是 warning,绝不拦写盘。"""
        kb = _seed_kb(tmp_path, """version: "0.2.0.dev0"
name: demo
datasets:
- name: loan
  source: loan
  primary_key: [loan_id]
  fields:
  - name: loan_id
    expression: {dialects: [{dialect: ANSI_SQL, expression: loan_id}]}
hierarchies:
- name: product_hierarchy
  dataset: loan
  levels: [loan_id]
metrics: []
""")
        manager = SemanticManager(kb)
        draft = await manager.create_draft(
            "demo", "metric", "upsert", "loan_count",
            {"expression": "COUNT(loan.loan_id)"})

        await manager.confirm_draft("demo", draft["id"], dialect="sqlite")

        assert "loan_count" in kb.semantics_path("demo").read_text(encoding="utf-8")

    async def test_misplaced_entity_blocks_write(self, tmp_path):
        """放错层 = 硬错(N3:解析层出声 + 写盘层拦下,与 masking 成对)。"""
        kb = _seed_kb(tmp_path, """version: "0.2.0.dev0"
name: demo
datasets:
- name: loan
  source: loan
  primary_key: [loan_id]
  fields:
  - name: loan_id
    expression: {dialects: [{dialect: ANSI_SQL, expression: loan_id}]}
metrics: []
entity: {display_name: 贷款}
""")
        before = kb.semantics_path("demo").read_text(encoding="utf-8")
        with pytest.raises(ValueError, match="文档根上的 entity"):
            await SemanticManager(kb).auto_apply(
                "demo", "dataset", "extra", {"source": "extra"})
        assert kb.semantics_path("demo").read_text(encoding="utf-8") == before

    async def test_detail_surfaces_advisory_warning_as_issue_item(self, tmp_path):
        """管理端展示面不过滤:被豁免的问题一样出现在 issue_items 里。"""
        kb = _seed_kb(tmp_path, _ADVISORY_YAML)
        detail = await SemanticManager(kb).detail("demo", dialect="sqlite")
        items = [i for i in detail["issue_items"]
                 if i["code"] == "entity_natural_key_missing"]
        assert len(items) == 1
        assert items[0]["target"] == {"kind": "entity", "name": "loan"}

    def test_git_precommit_gate_exempts_advisory_warning(self, tmp_path):
        kb = _seed_kb(tmp_path, _ADVISORY_YAML)
        lint = kb.semantics_lint("demo", dialect="sqlite")
        assert lint([kb.semantics_path("demo")]) == []

    def test_git_precommit_gate_still_refuses_structural_error(self, tmp_path):
        kb = _seed_kb(tmp_path, _STRUCTURAL_YAML)
        lint = kb.semantics_lint("demo", dialect="sqlite")
        issues = lint([kb.semantics_path("demo")])
        assert any("实体自然键「ghost」" in i for i in issues)

    def test_git_precommit_gate_still_refuses_legacy_warning(self, tmp_path):
        """分级只豁免点名名单:既有 warning 的 git 门禁判据不变。"""
        kb = _seed_kb(tmp_path, """version: "0.2.0.dev0"
name: demo
datasets:
- name: loan
  source: loan
  unique_keys: [[ghost_col]]
  fields:
  - name: loan_id
    expression: {dialects: [{dialect: ANSI_SQL, expression: loan_id}]}
metrics: []
""")
        lint = kb.semantics_lint("demo", dialect="sqlite")
        assert any("unique_keys" in i for i in lint([kb.semantics_path("demo")]))


def _git(repo, *args: str) -> str:
    import os
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        timeout=30, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).stdout


class TestGitCommitEndToEnd:
    @pytest.fixture
    def git_repo(self, tmp_path):
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"],
                       capture_output=True, text=True, timeout=30)
        _git(repo, "config", "user.name", "Tester")
        _git(repo, "config", "user.email", "tester@local")
        return repo

    async def test_advisory_warning_still_lands_in_git_history(self, git_repo):
        """端到端:语义缺口 warning 不拦 commit(提示得到 ≠ 拦下)。"""
        kb = KbService(git_repo, git_kb=True)
        (kb.kb_dir / "demo").mkdir(parents=True, exist_ok=True)
        kb.semantics_path("demo").write_text(_ADVISORY_YAML, encoding="utf-8")
        manager = SemanticManager(kb)
        draft = await manager.create_draft(
            "demo", "metric", "upsert", "loan_count",
            {"expression": "COUNT(loan.loan_id)"})

        await manager.confirm_draft("demo", draft["id"], dialect="sqlite")

        assert "loan_count" in kb.semantics_path("demo").read_text(
            encoding="utf-8")
        assert "confirm metric loan_count" in _git(git_repo, "log", "--oneline")
        # 提示仍在(展示面与门禁面分开)
        assert any("未声明自然键" in i for i in manager.issues("demo"))

    async def test_structural_error_never_reaches_git_history(self, git_repo):
        kb = KbService(git_repo, git_kb=True)
        (kb.kb_dir / "demo").mkdir(parents=True, exist_ok=True)
        kb.semantics_path("demo").write_text(_STRUCTURAL_YAML, encoding="utf-8")
        manager = SemanticManager(kb)
        draft = await manager.create_draft(
            "demo", "metric", "upsert", "loan_count",
            {"expression": "COUNT(loan.loan_id)"})

        with pytest.raises(ValueError, match="拒绝写入"):
            await manager.confirm_draft("demo", draft["id"], dialect="sqlite")

        assert "loan_count" not in kb.semantics_path("demo").read_text(
            encoding="utf-8")
        # 草稿创建自身有 git 记录;被拦的是**确认**(语义文件未增)
        assert "confirm metric loan_count" not in _git(git_repo, "log", "--oneline")


# ── validate 干跑与门禁一致 ────────────────────────────────


class TestValidateDraftGrading:
    def test_advisory_warning_keeps_ok_true(self):
        doc = _base_doc(loan_extra={
            "primary_key": [], "entity": {"display_name": "贷款"}})
        out = validate_draft(doc, kind="dataset", action="upsert", name="extra",
                             payload={"source": "extra"}, dialect="sqlite")
        assert out["errors"] == []
        assert [w["code"] for w in out["warnings"]] == [
            "entity_natural_key_missing"]
        assert out["ok"] is True

    def test_structural_error_keeps_ok_false(self):
        doc = _base_doc(loan_extra={"entity": {"natural_keys": ["ghost"]}})
        out = validate_draft(doc, kind="dataset", action="upsert", name="extra",
                             payload={"source": "extra"}, dialect="sqlite")
        assert [e["code"] for e in out["errors"]] == ["entity_natural_key"]
        assert out["ok"] is False

    def test_hierarchy_error_keeps_ok_false(self):
        doc = _base_doc(model_extra={"hierarchies": [
            {"name": "geo", "dataset": "nope",
             "levels": ["loan_id", "amount"]}]})
        out = validate_draft(doc, kind="dataset", action="upsert", name="extra",
                             payload={"source": "extra"}, dialect="sqlite")
        assert [e["code"] for e in out["errors"]] == ["hierarchy_dataset_missing"]
        assert out["ok"] is False

    def test_validate_is_side_effect_free(self):
        doc = _base_doc(loan_extra={
            "primary_key": [], "entity": {"display_name": "贷款"}})
        before = yaml.safe_dump(doc, sort_keys=False)
        validate_draft(doc, kind="dataset", action="upsert", name="extra",
                       payload={"source": "extra"}, dialect="sqlite")
        assert yaml.safe_dump(doc, sort_keys=False) == before
