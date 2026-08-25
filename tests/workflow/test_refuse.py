"""语义优先(Phase A)拒绝节点测试:no_model / 编译 MISS 草稿 / 冲突检测 / 确认重答。

对应 semantic-first 架构文档 §4.2 / §5(refusal、no_model 状态位)。
"""

from __future__ import annotations

import json

import yaml

import pytest

from trove.core.config import AgentConfig
from trove.services.kb.service import KbService
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)
from trove.workflow.nodes.refuse import make_refuse
from trove.workflow.state import WorkflowState


def _demo_model() -> SemanticModel:
    """含 loan/account 数据集 + 记录数 metric,无聚合金额 metric。"""
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
    defaults = {"session_id": "s1", "question": "平均贷款金额是多少?", "lang": "zh"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


@pytest.fixture
def kb(tmp_path):
    return KbService(tmp_path / "proj")


METRIC_DRAFT_YAML = """\
draft:
  kind: metric
  name: avg_loan_amount
  expression: AVG(loan.amount)
  synonyms: [平均贷款金额, 贷款平均金额]
  definition: 贷款金额的平均值
  datasets: [loan]
"""


class TestNoModelRefusal:
    async def test_no_model_refusal_is_deterministic(self, kb):
        """无语义模型(决策 2/3)→ 确定性拒绝文案,零 LLM 调用。"""
        class ExplodingLLM:
            async def chat(self, model, messages, **kwargs):
                raise AssertionError("no_model refusal must not call LLM")

        node = make_refuse(ExplodingLLM(), AgentConfig(target="mock/model"), kb=kb)
        out = await node(make_state(no_model=True))
        assert out["clarification_question"]
        assert "/kb init" in out["clarification_question"]
        assert out["refusal"]["reason"] == "no_model"

    async def test_no_model_skips_draft_write(self, kb):
        """no_model 拒绝不写任何草稿。"""
        from trove.services.semantic_layer.manage import SemanticManager
        node = make_refuse(ScriptedLLM([]), AgentConfig(target="mock/model"), kb=kb)
        await node(make_state(no_model=True, datasource="demo"))
        assert not SemanticManager(kb).drafts("demo")["pending"]


class TestUncoveredRefusal:
    async def test_refusal_drafts_metric_and_writes_pending(self, kb):
        """编译 MISS → LLM 草拟 metric → 写入 semantic_drafts.yml pending。"""
        from trove.services.semantic_layer.manage import SemanticManager
        node = make_refuse(
            ScriptedLLM([METRIC_DRAFT_YAML]),
            AgentConfig(target="mock/model"),
            kb=kb, semantic_layer=FakeProvider(_demo_model()),
        )
        out = await node(make_state(
            datasource="demo",
            refusal={"reason": "uncovered", "question": "平均贷款金额是多少?",
                     "plan": {"aggregation": "AVG(loan.amount)",
                              "answer_columns": ["AVG(loan.amount)"]}},
        ))
        assert "avg_loan_amount" in out["clarification_question"]
        assert out["refusal"]["conflict"] is False
        entry = out["refusal"]["draft_entry"]
        assert entry["status"] == "pending"
        assert entry["kind"] == "metric"
        pending = SemanticManager(kb).drafts("demo")["pending"]
        assert len(pending) == 1
        assert pending[0]["payload"]["expression"] == "AVG(loan.amount)"

    async def test_refusal_same_name_conflict_no_write(self, kb):
        """与现有模型同名 → 冲突,不写库,文案提示人工补充。"""
        from trove.services.semantic_layer.manage import SemanticManager
        conflict_yaml = METRIC_DRAFT_YAML.replace("avg_loan_amount", "number of loan records")
        node = make_refuse(
            ScriptedLLM([conflict_yaml]),
            AgentConfig(target="mock/model"),
            kb=kb, semantic_layer=FakeProvider(_demo_model()),
        )
        out = await node(make_state(
            datasource="demo",
            refusal={"reason": "uncovered", "question": "q",
                     "plan": {"aggregation": "COUNT(loan.loan_id)",
                              "answer_columns": ["COUNT(loan.loan_id)"]}},
        ))
        assert out["refusal"]["conflict"] is True
        assert not SemanticManager(kb).drafts("demo")["pending"]

    async def test_refusal_with_compile_miss_reason_in_message(self, kb):
        """refusal 带 compile_miss 分因 → 拒绝文案具体到缺失组件,reason 契约不变。"""
        node = make_refuse(
            ScriptedLLM([METRIC_DRAFT_YAML]),
            AgentConfig(target="mock/model"),
            kb=kb, semantic_layer=FakeProvider(_demo_model()),
        )
        out = await node(make_state(
            datasource="demo",
            refusal={
                "reason": "uncovered", "question": "q",
                "plan": {"aggregation": "AVG(loan.amount)",
                         "answer_columns": ["AVG(loan.amount)"]},
                "compile_miss": {"reason": "no_metric_match",
                                 "component": "AVG(loan.amount)"},
            },
        ))
        # 用户文案具体到「缺哪个组件」(不再笼统 uncovered)
        assert "no_metric_match" in out["clarification_question"]
        assert "AVG(loan.amount)" in out["clarification_question"]
        # 上游契约不变:refusal["reason"] 仍是原始 reason,供机器匹配/聚合
        assert out["refusal"]["reason"] == "uncovered"

    async def test_refusal_unparseable_expr_conflict(self, kb):
        """表达式不可解析 → 冲突,不写库。"""
        from trove.services.semantic_layer.manage import SemanticManager
        bad = METRIC_DRAFT_YAML.replace("AVG(loan.amount)", "AVG(loan.amount")
        node = make_refuse(
            ScriptedLLM([bad]),
            AgentConfig(target="mock/model"),
            kb=kb, semantic_layer=FakeProvider(_demo_model()),
        )
        out = await node(make_state(
            datasource="demo",
            refusal={"reason": "uncovered", "question": "q", "plan": {}},
        ))
        assert out["refusal"]["conflict"] is True
        assert not SemanticManager(kb).drafts("demo")["pending"]

    async def test_refusal_undeclared_dataset_conflict(self, kb):
        """metric 引用未声明数据集 → 冲突,不写库。"""
        from trove.services.semantic_layer.manage import SemanticManager
        bad = METRIC_DRAFT_YAML.replace("datasets: [loan]", "datasets: [ghost]")
        node = make_refuse(
            ScriptedLLM([bad]),
            AgentConfig(target="mock/model"),
            kb=kb, semantic_layer=FakeProvider(_demo_model()),
        )
        out = await node(make_state(
            datasource="demo",
            refusal={"reason": "uncovered", "question": "q", "plan": {}},
        ))
        assert out["refusal"]["conflict"] is True
        assert not SemanticManager(kb).drafts("demo")["pending"]

    async def test_refusal_unparseable_llm_output_no_write(self, kb):
        """LLM 输出无法解析 → 无草稿,仍产出「缺少声明」文案,不写库。"""
        from trove.services.semantic_layer.manage import SemanticManager
        node = make_refuse(
            ScriptedLLM(["not yaml at all"]),
            AgentConfig(target="mock/model"),
            kb=kb, semantic_layer=FakeProvider(_demo_model()),
        )
        out = await node(make_state(
            datasource="demo",
            refusal={"reason": "uncovered", "question": "q", "plan": {}},
        ))
        assert out["refusal"]["draft"] is None
        assert "缺少" in out["clarification_question"]
        assert not SemanticManager(kb).drafts("demo")["pending"]

    async def test_refusal_field_draft(self, kb):
        """字段型草稿(缺失维度)→ 写入 pending,payload 对齐 confirm。"""
        from trove.services.semantic_layer.manage import SemanticManager
        field_yaml = """\
draft:
  kind: field
  name: loan.region
  expression: region
  synonyms: [地区]
  definition: 贷款所属地区
  datatype: String
"""
        node = make_refuse(
            ScriptedLLM([field_yaml]),
            AgentConfig(target="mock/model"),
            kb=kb, semantic_layer=FakeProvider(_demo_model()),
        )
        out = await node(make_state(
            datasource="demo",
            refusal={"reason": "uncovered", "question": "各地区贷款金额?", "plan": {}},
        ))
        assert out["refusal"]["conflict"] is False
        pending = SemanticManager(kb).drafts("demo")["pending"]
        assert len(pending) == 1
        assert pending[0]["kind"] == "field"
        assert pending[0]["payload"]["expression"] == "region"


class TestRefuseConfirmReanswerLoop:
    async def test_confirm_draft_recompile_loop(self, tmp_path):
        """确认草稿 → semantics.yml 落地 → 重答时编译器可命中(闭环)。"""
        from trove.services.semantic_layer.manage import SemanticManager
        from trove.services.semantic_layer.provider import SemanticLayerProvider
        from trove.workflow.nodes.planner import make_planner

        kb = KbService(tmp_path / "proj")
        ds_dir = kb.kb_dir / "demo"
        ds_dir.mkdir(parents=True, exist_ok=True)
        # 初始模型:只有 loan 数据集 + 字段,无聚合 metric
        (kb.semantics_path("demo")).write_text(
            "semantic_model:\n"
            "  - name: demo\n"
            "    datasets:\n"
            "      - name: loan\n"
            "        source: loan\n"
            "        primary_key: [loan_id]\n"
            "        fields:\n"
            "          - name: loan_id\n"
            "            expression: {dialects: [{dialect: ANSI_SQL, expression: loan_id}]}\n"
            "          - name: amount\n"
            "            expression: {dialects: [{dialect: ANSI_SQL, expression: amount}]}\n",
            encoding="utf-8",
        )
        provider = SemanticLayerProvider(
            tmp_path / "semantic", "demo",
            kb_semantics_path=kb.semantics_path("demo"),
        )
        model_before = provider.model()
        assert model_before is not None
        assert not model_before.metrics

        # refuse:起草 + 写入 pending
        refuse_node = make_refuse(
            ScriptedLLM([METRIC_DRAFT_YAML]),
            AgentConfig(target="mock/model"),
            kb=kb, semantic_layer=provider,
        )
        out = await refuse_node(make_state(
            datasource="demo",
            refusal={"reason": "uncovered", "question": "平均贷款金额?",
                     "plan": {"aggregation": "AVG(loan.amount)",
                              "answer_columns": ["AVG(loan.amount)"]}},
        ))
        manager = SemanticManager(kb)
        pending = manager.drafts("demo")["pending"]
        assert len(pending) == 1
        assert out["refusal"]["conflict"] is False

        # 确认 → 模型覆盖
        await manager.confirm_draft("demo", pending[0]["id"], dialect="sqlite")
        assert manager.model("demo", dialect="sqlite").metrics

        # 重答:provider 重载 → 编译命中
        class FakeEnabledProvider:
            enabled = True

            def model(self):
                return provider.model()

        plan = {"tables": ["loan"], "aggregation": "AVG(loan.amount)",
                "answer_columns": ["AVG(loan.amount)"], "conditions": []}
        planner = make_planner(
            ScriptedLLM([json.dumps(plan)]),
            AgentConfig(target="mock/model", semantic_first=True),
            semantic_layer=FakeEnabledProvider(),
        )
        update = await planner(make_state(
            question="平均贷款金额?", matched_tables=["loan"], datasource="demo"))
        assert update["compiled"] is True
        assert "AVG(loan.amount)" in update["compiled_sql"]


_SCHOOL_SEMANTICS = """\
semantic_model:
- name: school
  datasets:
  - name: students
    source: students
    primary_key: [id]
    fields:
    - name: id
      expression: {dialects: [{dialect: ANSI_SQL, expression: id}]}
    - name: grade
      expression: {dialects: [{dialect: ANSI_SQL, expression: grade}]}
    - name: county
      expression: {dialects: [{dialect: ANSI_SQL, expression: county}]}
    ai_context:
      synonyms: [student]
  metrics:
  - name: average grade
    expression: {dialects: [{dialect: ANSI_SQL, expression: AVG(students.grade)}]}
"""


class ExhaustingLLM:
    """脚本化回复;耗尽后抛异常(模拟真实网关故障,避免死循环)。"""

    def __init__(self, responses):
        self._r = iter(responses)
        self.calls = 0

    async def chat(self, model, messages, **kwargs):
        self.calls += 1
        try:
            return next(self._r)
        except StopIteration:
            raise RuntimeError("mock LLM exhausted") from None


class TestReflectionGraphRouting:
    async def _provider(self, tmp_path, kb):
        from trove.services.semantic_layer.provider import SemanticLayerProvider
        kb.kb_dir.mkdir(parents=True, exist_ok=True)
        (kb.kb_dir / "demo").mkdir(parents=True, exist_ok=True)
        (kb.semantics_path("demo")).write_text(_SCHOOL_SEMANTICS, encoding="utf-8")
        return SemanticLayerProvider(
            tmp_path / "semantic", "demo",
            kb_semantics_path=kb.semantics_path("demo"),
            table_exists=lambda t: True, dialect="sqlite",
        )

    def _services(self, llm, catalog, connectors, kb, provider):
        from trove.workflow.graphs import GraphServices
        return GraphServices(
            llm=llm, catalog=catalog, connectors=connectors,
            config=AgentConfig(target="mock/model", semantic_first=True, language="zh"),
            kb=kb, semantic_layer=provider,
        )

    async def test_uncovered_routes_to_refuse(self, tmp_path, sqlite_registry, catalog):
        """图路由:编译 MISS → refuse 节点 → 反问文案,不执行不生成。"""
        from trove.services.kb.service import KbService
        from trove.workflow.graphs import build_graphs
        from trove.workflow.state import WorkflowState

        kb = KbService(tmp_path / "proj")
        provider = await self._provider(tmp_path, kb)
        plan = {"tables": ["students"], "aggregation": "SUM(students.ghost)",
                "answer_columns": ["SUM(students.ghost)"], "conditions": []}
        draft = """\
draft:
  kind: metric
  name: sum_of_grades
  expression: SUM(students.grade)
  synonyms: [total grade, total grades]
  definition: total of all student grades
  datasets: [students]
"""
        # intent → planner → refuse 草稿三处 LLM 调用
        llm = ExhaustingLLM(["query", json.dumps(plan), draft])
        graphs = build_graphs(
            self._services(llm, catalog, sqlite_registry, kb, provider),
            multi_candidate=False, planner=True, agentic=False,
        )
        final = await graphs["reflection"].ainvoke(WorkflowState(
            session_id="s1", question="what is the total ghost sum for students?",
            lang="en", datasource="demo"))
        assert final["refusal"]["reason"] == "uncovered"
        assert final["refusal"]["conflict"] is False
        assert final["sql"] == ""
        assert final["row_count"] == -1
        assert "sum_of_grades" in (final["clarification_question"] or "")
        assert llm.calls == 3  # intent + planner + refuse 草稿,无 gen_sql/reflect

    async def test_covered_plan_not_routed_to_refuse(self):
        """覆盖内计划 → _route_after_planner 走 gen_sql(不误拒)。"""
        from trove.workflow.graphs import _route_after_planner
        from trove.workflow.state import WorkflowState

        covered = WorkflowState(session_id="s1", question="q", compiled=True)
        assert _route_after_planner(covered) == "gen_sql"

        refused = WorkflowState(session_id="s1", question="q",
                                refusal={"reason": "uncovered", "question": "q"})
        assert _route_after_planner(refused) == "refuse"

        no_model = WorkflowState(session_id="s1", question="q", no_model=True)
        assert _route_after_planner(no_model) == "refuse"

    async def test_semantic_gates_short_circuit_to_refuse(self):
        """linking 后语义门:no_model / refusal 短路到 refuse,否则走原通道。"""
        from trove.workflow.graphs import (
            _route_semantic_gate_after_linking_fast_match,
            _route_semantic_gate_after_linking_gen_sql,
        )
        from trove.workflow.state import WorkflowState

        assert _route_semantic_gate_after_linking_fast_match(
            WorkflowState(session_id="s1", question="q", no_model=True)) == "refuse"
        assert _route_semantic_gate_after_linking_gen_sql(
            WorkflowState(session_id="s1", question="q",
                          refusal={"reason": "uncovered", "question": "q"})) == "refuse"
        assert _route_semantic_gate_after_linking_fast_match(
            WorkflowState(session_id="s1", question="q")) == "fast_match"
        assert _route_semantic_gate_after_linking_gen_sql(
            WorkflowState(session_id="s1", question="q")) == "gen_sql"
