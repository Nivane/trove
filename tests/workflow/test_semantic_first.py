"""语义优先(Phase A)测试:semantic_context 渲染 / dataset 锚定 / 零 DDL 泄漏 /
无 fallback / 无模型拒绝 / planner MISS→refuse。

对应 semantic-first 架构文档 §4.1 / §4.3 / §5。
"""

from __future__ import annotations

import json

import pytest

from trove.core.config import AgentConfig
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)
from trove.workflow.nodes.schema_linking import make_schema_linking
from trove.workflow.state import WorkflowState


def _students_model() -> SemanticModel:
    f = lambda name: SemanticField(name=name, expression=name)  # noqa: E731
    return SemanticModel(
        name="school",
        datasets=[
            SemanticDataset(name="students", primary_key=["id"], fields=[
                f("id"), f("grade"),
            ], synonyms=["student"], description="student records"),
        ],
        metrics=[
            SemanticMetric("average grade", "AVG(students.grade)",
                           datasets=["students"]),
        ],
    )


class FakeProvider:
    enabled = True

    def __init__(self, model):
        self._model = model
        self.terms = []

    def model(self):
        return self._model

    def terms_for(self, query, tables=None, all_tables=None):
        return list(self.terms)

    def field_hits(self, question, tables=None):
        return []


class ScriptedLLM:
    def __init__(self, responses):
        self._responses = iter(responses)

    async def chat(self, model, messages, **kwargs):
        return next(self._responses)


def make_state(**kwargs) -> WorkflowState:
    defaults = {"session_id": "s1", "question": "students average grade by county"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


def _node(connectors=None, provider=None):
    return make_schema_linking(
        kb=None, connectors=connectors, semantic_layer=provider,
    )


class TestSemanticFirstLinking:
    async def test_no_model_refuses_when_no_layer(self, sqlite_registry):
        """语义优先 + 无语义层 → no_model 拒绝(决策 2/3),不静默降级裸表。"""
        node = _node(connectors=sqlite_registry, provider=None)
        out = await node(make_state())
        assert out["no_model"] is True
        assert out["matched_tables"] == []
        assert out["semantic_context"] == ""

    async def test_semantic_context_no_physical_leak(self, sqlite_registry):
        """semantic_context 只含模型声明,不泄漏物理列/统计/样本/join hints。"""
        node = _node(connectors=sqlite_registry,
                     provider=FakeProvider(_students_model()))
        out = await node(make_state())
        ctx = out["semantic_context"]
        assert "Dataset: students" in ctx
        assert "average grade = AVG(students.grade)" in ctx
        # 物理 schema 启发全部不得出现
        assert "Approximate rows" not in ctx
        assert "top values" not in ctx
        assert "Join hints" not in ctx
        assert "Stats" not in ctx
        # 未声明的物理列不得出现(模型只声明 id/grade)
        assert "county" not in ctx
        assert "name" not in ctx

    async def test_matched_datasets_anchoring(self, sqlite_registry):
        """表锚定改为 dataset 锚定:matched_tables = 匹配的 dataset 名。"""
        node = _node(connectors=sqlite_registry,
                     provider=FakeProvider(_students_model()))
        out = await node(make_state())
        assert out["matched_tables"] == ["students"]
        assert out["schema_context"] == out["semantic_context"]
        assert out["link_detail"]["semantic_first"] is True
        assert out["link_detail"]["matched_datasets"] == ["students"]

    async def test_zero_match_refuses_no_fallback(self, sqlite_registry):
        """零命中 = 未覆盖 = 拒绝;无任何 fallback 兜底(决策 4)。"""
        node = _node(connectors=sqlite_registry,
                     provider=FakeProvider(_students_model()))
        out = await node(make_state(question="totally unrelated query about weather"))
        assert out["matched_tables"] == []
        assert out["refusal"] is not None
        assert out["refusal"]["reason"] == "no_semantic_match"
        assert out["schema_context"].startswith("No semantic model matched")

    async def test_legacy_catalog_path_removed(self, sqlite_registry):
        """Phase B:旧裸表路径已从查询图物理移除——语义层缺失即拒绝,无对照路径。"""
        node = _node(connectors=sqlite_registry, provider=None)
        out = await node(make_state(question="students average grade by county"))
        assert out["no_model"] is True
        assert out["matched_tables"] == []
        assert "legacy" not in out["link_detail"]


class TestPlannerSemanticFirst:
    @staticmethod
    def _demo_model():
        f = lambda name: SemanticField(name=name, expression=name)  # noqa: E731
        return SemanticModel(
            name="fin",
            datasets=[
                SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                    f("loan_id"), f("account_id"), f("amount"), f("status")]),
                SemanticDataset(name="account", primary_key=["account_id"], fields=[
                    f("account_id"), f("district_id")]),
            ],
            relationships=[
                SemanticRelationship("loan_to_account", "loan", "account",
                                     from_columns=["account_id"], to_columns=["account_id"]),
            ],
            metrics=[
                SemanticMetric("number of loan records", "COUNT(loan.loan_id)",
                               datasets=["loan"]),
            ],
        )

    async def test_planner_miss_emits_refusal_signal(self):
        """语义优先:编译 MISS + 真实意图 → refusal 信号(不再静默降级裸表)。"""
        from trove.workflow.nodes.planner import make_planner

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        node = make_planner(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "sum(loan.ghost)",
                "answer_columns": ["sum(loan.ghost)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model", semantic_first=True),
            semantic_layer=FakeProvider(self._demo_model()),
        )
        out = await node(make_state(question="贷款总额?", matched_tables=["loan"]))
        assert "compiled" not in out
        assert out["refusal"] is not None
        assert out["refusal"]["reason"] == "uncovered"
        assert out["refusal"]["plan"]["aggregation"] == "sum(loan.ghost)"
        # MISS 结构化分因透出(不再被丢弃成笼统 uncovered):reason slug + 组件
        assert out["refusal"]["compile_miss"]["reason"] == "no_metric_match"
        assert "sum(loan.ghost)" in out["refusal"]["compile_miss"]["component"]

    async def test_planner_miss_without_intent_does_not_refuse(self):
        """退化/空洞计划不拒绝 → 照常走 gen_sql(不误拒)。"""
        from trove.workflow.nodes.planner import make_planner

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        node = make_planner(
            ScriptedLLM([json.dumps({"tables": ["loan"]})]),
            AgentConfig(target="mock/model", semantic_first=True),
            semantic_layer=FakeProvider(self._demo_model()),
        )
        out = await node(make_state(question="?", matched_tables=["loan"]))
        assert "compiled" not in out
        assert "refusal" not in out

    async def test_planner_covered_compiles(self):
        """覆盖内问题仍走编译器 → 权威 SQL(语义优先后路径不变)。"""
        from trove.workflow.nodes.planner import make_planner

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        node = make_planner(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model", semantic_first=True),
            semantic_layer=FakeProvider(self._demo_model()),
        )
        out = await node(make_state(question="how many loans?", matched_tables=["loan"]))
        assert out["compiled"] is True
        assert out["compiled_sql"] == "SELECT COUNT(loan.loan_id)\nFROM loan"

    @staticmethod
    def _demo_model_with_date():
        """loan 带 is_time 的 date 字段 → 时间绑定可判定。"""
        f = lambda name, **kw: SemanticField(name=name, expression=name, **kw)  # noqa: E731
        return SemanticModel(
            name="fin",
            datasets=[
                SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                    f("loan_id"), f("account_id"), f("amount"),
                    f("date", datatype="Date", is_time=True),
                ]),
            ],
            metrics=[
                SemanticMetric("number of loan records", "COUNT(loan.loan_id)",
                               datasets=["loan"]),
            ],
        )

    async def test_planner_time_binding_injects_range_condition(self):
        """P1-4:time_context + 唯一时间维度 → 确定性注入区间条件,编译 SQL 带过滤。"""
        from trove.workflow.nodes.planner import make_planner

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        node = make_planner(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model", semantic_first=True),
            semantic_layer=FakeProvider(self._demo_model_with_date()),
        )
        out = await node(make_state(
            question="最近7天有多少贷款?", matched_tables=["loan"],
            time_context="2025-01-01 ~ 2025-01-15"))
        assert out["compiled"] is True
        assert "loan.date >= '2025-01-01'" in out["compiled_sql"]
        assert "loan.date <= '2025-01-15'" in out["compiled_sql"]
        # 计划文本也带该条件(未覆盖路径 gen_sql 同样看到)
        assert "loan.date >=" in out["plan"]

    async def test_planner_time_binding_skips_when_ambiguous(self):
        """P1-4:多个时间字段 → 无法判定,不注入(plan 原样,不猜)。"""
        from trove.workflow.nodes.planner import make_planner

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        f = lambda name, **kw: SemanticField(name=name, expression=name, **kw)  # noqa: E731
        model = SemanticModel(
            name="fin",
            datasets=[SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                f("loan_id"),
                f("created_at", datatype="Date", is_time=True),
                f("updated_at", datatype="Date", is_time=True),
            ])],
            metrics=[SemanticMetric("number of loan records", "COUNT(loan.loan_id)",
                                    datasets=["loan"])],
        )
        node = make_planner(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model", semantic_first=True),
            semantic_layer=FakeProvider(model),
        )
        out = await node(make_state(
            question="最近7天有多少贷款?", matched_tables=["loan"],
            time_context="2025-01-01 ~ 2025-01-15"))
        assert out["compiled"] is True
        assert "WHERE" not in out["compiled_sql"]  # 未注入区间条件

    async def test_planner_writes_compile_meta_both_paths(self):
        """编译决策观测:命中与 MISS 都写 compile_meta(eval hit-rate 闭环数据源)。"""
        from trove.workflow.nodes.planner import make_planner

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        # 命中路径
        node = make_planner(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model", semantic_first=True),
            semantic_layer=FakeProvider(self._demo_model()),
        )
        out = await node(make_state(question="how many loans?", matched_tables=["loan"]))
        assert out["compile_meta"]["outcome"] == "compiled"
        assert out["compile_meta"]["plan_typed"] is True
        assert out["compile_meta"]["semantic_layer"] is True
        assert out["compile_meta"]["miss_reason"] == ""

        # MISS 路径
        node_miss = make_planner(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "sum(loan.ghost)",
                "answer_columns": ["sum(loan.ghost)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model", semantic_first=True),
            semantic_layer=FakeProvider(self._demo_model()),
        )
        out_miss = await node_miss(
            make_state(question="贷款总额?", matched_tables=["loan"]))
        assert out_miss["compile_meta"]["outcome"] == "miss"
        assert out_miss["compile_meta"]["miss_reason"] == "no_metric_match"
        assert "sum(loan.ghost)" in out_miss["compile_meta"]["miss_component"]

    async def test_planner_compile_meta_no_semantic_layer(self):
        """无语义层接线时:不编译、不拒绝,compile_meta 记 no_semantic_layer。"""
        from trove.workflow.nodes.planner import make_planner

        node = make_planner(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model", semantic_first=True),
            semantic_layer=None,
        )
        out = await node(make_state(question="how many loans?", matched_tables=["loan"]))
        assert "compiled" not in out
        assert out["compile_meta"]["outcome"] == "miss"
        assert out["compile_meta"]["miss_reason"] == "no_semantic_layer"
        assert out["compile_meta"]["semantic_layer"] is False
