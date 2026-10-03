"""语义优先(Phase A)测试:semantic_context 渲染 / dataset 锚定 / 零 DDL 泄漏 /
无 fallback / 无模型拒绝 / query_sketch MISS→refuse。

对应 semantic-first 架构文档 §4.1 / §4.3 / §5。
"""

from __future__ import annotations

import json


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

    async def test_enum_display_rendered_in_context(self, sqlite_registry):
        """enum_display 渲染进 semantic_context(值映射,不是只有个数)。"""
        f = lambda name: SemanticField(name=name, expression=name)  # noqa: E731
        model = SemanticModel(
            name="fin",
            datasets=[
                SemanticDataset(name="client", primary_key=["client_id"], fields=[
                    f("client_id"),
                    SemanticField(name="gender", expression="gender",
                                  datatype="String", semantic_role="enum",
                                  enum_display={"F": "female", "M": "male"}),
                ]),
            ],
            metrics=[
                SemanticMetric("number of clients", "COUNT(client.client_id)",
                               datasets=["client"]),
            ],
        )
        node = _node(connectors=sqlite_registry, provider=FakeProvider(model))
        out = await node(make_state(question="male clients"))
        ctx = out["semantic_context"]
        assert "role=enum" in ctx
        assert "enum {F=female, M=male}" in ctx

    @staticmethod
    def _enum_model(enum_display: dict) -> SemanticModel:
        f = lambda name: SemanticField(name=name, expression=name)  # noqa: E731
        return SemanticModel(
            name="fin",
            datasets=[
                SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                    f("loan_id"), f("amount"), f("client_id")]),
                SemanticDataset(name="client", primary_key=["client_id"], fields=[
                    f("client_id"),
                    SemanticField(name="gender", expression="gender",
                                  datatype="String", semantic_role="enum",
                                  enum_display=enum_display),
                ]),
            ],
            relationships=[
                SemanticRelationship("loan_to_client", "loan", "client",
                                     from_columns=["client_id"],
                                     to_columns=["client_id"]),
            ],
            metrics=[
                SemanticMetric("loan total", "SUM(loan.amount)", datasets=["loan"]),
            ],
        )

    async def test_enum_display_value_anchors_dataset(self, sqlite_registry):
        """B2:问题点名 enum_display 展示值("female")→ 该数据集入 matched,
        即使问题里没有它的名称/synonym/字段名(0483 型问题的唯一线索)。"""
        model = self._enum_model({"F": "female", "M": "male"})
        node = _node(connectors=sqlite_registry, provider=FakeProvider(model))
        out = await node(make_state(question="total loans for female borrowers"))
        # loan 名称命中(3.0)在前,client 靠 enum 展示值命中(2.5)跟入
        assert out["matched_tables"] == ["loan", "client"]

    async def test_short_enum_display_values_do_not_anchor(self, sqlite_registry):
        """长度 <3 的枚举展示值(F/M)不参与锚定:英文问句里几乎必然噪声
        命中(B2 的长度闸)。"""
        model = self._enum_model({"1": "F", "2": "M"})
        node = _node(connectors=sqlite_registry, provider=FakeProvider(model))
        out = await node(make_state(question="total loans for female borrowers"))
        assert out["matched_tables"] == ["loan"]

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


class TestQuerySketchSemanticFirst:
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

    @staticmethod
    def _m2m_model():
        """M:N 关系(loan↔account 多对多):联路径在,但行倍增 → fan_out 硬 MISS。"""
        from dataclasses import replace

        return replace(
            TestQuerySketchSemanticFirst._demo_model(),
            relationships=[
                SemanticRelationship("loan_accounts", "loan", "account",
                                     from_columns=["account_id"],
                                     to_columns=["account_id"],
                                     cardinality="M:N"),
            ],
        )

    async def test_query_sketch_hard_miss_emits_refusal_signal(self):
        """语义优先 + 分级逃生梯(A2):硬 MISS(fan_out 行倍增)+ 真实意图
        → refusal 信号(结构性缺口不逃生,不再静默降级裸表)。"""
        from trove.workflow.nodes.query_sketch import make_query_sketch

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        node = make_query_sketch(
            ScriptedLLM([json.dumps({
                "tables": ["loan", "account"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"],
                "conditions": [{"field": "account.district_id", "op": "=", "value": "1"}],
            })]),
            AgentConfig(target="mock/model"),
            semantic_layer=FakeProvider(self._m2m_model()),
        )
        out = await node(make_state(question="各账户的贷款数?", matched_tables=["loan"]))
        assert "compiled" not in out
        assert out["refusal"] is not None
        assert out["refusal"]["reason"] == "uncovered"
        assert out["refusal"]["plan"]["aggregation"] == "count(loan.loan_id)"
        # MISS 结构化分因透出(不再被丢弃成笼统 uncovered):reason slug + 组件
        assert out["refusal"]["compile_miss"]["reason"] == "fan_out"
        assert out["refusal"]["compile_miss"]["component"]
        # 硬度分级正交可见(不改变 outcome 值域)
        assert out["compile_meta"]["miss_class"] == "hard"

    async def test_query_sketch_soft_miss_passes_through(self):
        """A2 分级逃生梯下沿:软 MISS(词表缺口,无骨架可保真)→ 不拒绝、
        不产编译产物,plan 文本照常注入 gen_sql 按计划补齐。"""
        from trove.workflow.nodes.query_sketch import make_query_sketch

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        node = make_query_sketch(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "sum(loan.ghost)",
                "answer_columns": ["sum(loan.ghost)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model"),
            semantic_layer=FakeProvider(self._demo_model()),
        )
        out = await node(make_state(question="贷款总额?", matched_tables=["loan"]))
        assert "refusal" not in out
        assert "compiled" not in out
        assert "compile_partial" not in out
        assert out["compile_meta"]["outcome"] == "miss"
        assert out["compile_meta"]["miss_class"] == "soft"
        assert out["compile_meta"]["miss_reason"] == "no_metric_match"
        assert "sum(loan.ghost)" in out["compile_meta"]["miss_component"]
        # plan 文本照常注入(gen_sql 拿它补齐)
        assert "sum(loan.ghost)" in out["plan"]

    async def test_query_sketch_miss_without_intent_does_not_refuse(self):
        """退化/空洞计划不拒绝 → 照常走 gen_sql(不误拒)。"""
        from trove.workflow.nodes.query_sketch import make_query_sketch

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        node = make_query_sketch(
            ScriptedLLM([json.dumps({"tables": ["loan"]})]),
            AgentConfig(target="mock/model"),
            semantic_layer=FakeProvider(self._demo_model()),
        )
        out = await node(make_state(question="?", matched_tables=["loan"]))
        assert "compiled" not in out
        assert "refusal" not in out

    async def test_query_sketch_covered_compiles(self):
        """覆盖内问题仍走编译器 → 权威 SQL(语义优先后路径不变)。"""
        from trove.workflow.nodes.query_sketch import make_query_sketch

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        node = make_query_sketch(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model"),
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

    async def test_query_sketch_time_binding_injects_range_condition(self):
        """P1-4:time_context + 唯一时间维度 → 确定性注入区间条件,编译 SQL 带过滤。"""
        from trove.workflow.nodes.query_sketch import make_query_sketch

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        node = make_query_sketch(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model"),
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

    async def test_query_sketch_time_binding_skips_when_ambiguous(self):
        """P1-4:多个时间字段 → 无法判定,不注入(plan 原样,不猜)。"""
        from trove.workflow.nodes.query_sketch import make_query_sketch

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
        node = make_query_sketch(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model"),
            semantic_layer=FakeProvider(model),
        )
        out = await node(make_state(
            question="最近7天有多少贷款?", matched_tables=["loan"],
            time_context="2025-01-01 ~ 2025-01-15"))
        assert out["compiled"] is True
        assert "WHERE" not in out["compiled_sql"]  # 未注入区间条件

    async def test_query_sketch_soft_miss_partial_proceeds(self):
        """分级逃生梯:软 MISS(未声明的枚举值)不再拒绝——骨架注入 plan,
        compiled=True + compile_partial=True,回答照常生成。"""
        from trove.workflow.nodes.query_sketch import make_query_sketch

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        # loan 带 enum_display 的 status 字段:值 'Z' 不在词表 → enum_value_unresolved
        f = lambda name, **kw: SemanticField(name=name, expression=name, **kw)  # noqa: E731
        model = SemanticModel(
            name="fin",
            datasets=[SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                f("loan_id"), f("account_id"), f("amount"),
                f("status", semantic_role="enum", enum_display={"A": "active", "B": "closed"}),
            ])],
            metrics=[
                SemanticMetric("number of loan records", "COUNT(loan.loan_id)",
                               datasets=["loan"]),
            ],
        )
        node = make_query_sketch(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"],
                "conditions": [{"field": "loan.status", "op": "=", "value": "Z"}],
            })]),
            AgentConfig(target="mock/model"),
            semantic_layer=FakeProvider(model),
        )
        out = await node(make_state(question="状态为 Z 的贷款数?", matched_tables=["loan"]))
        # 不拒绝:照常走 gen_sql(骨架路径)
        assert "refusal" not in out
        assert out["compiled"] is True
        assert out["compile_partial"] is True
        # 骨架 SQL 含权威部分(度量投影);未覆盖条件留给 LLM 补
        assert "COUNT(loan.loan_id)" in out["compiled_sql"]
        assert out["compile_meta"]["outcome"] == "partial"
        # plan 文本带骨架提示块 + 未覆盖组件清单
        assert "Compiled skeleton (authoritative" in out["plan"]
        assert "enum_value_unresolved: loan.status" in out["plan"]
        # 未覆盖组件结构化记录(学习/归因)
        assert out["compile_misses"] == [
            {"reason": "enum_value_unresolved", "component": "loan.status"}]

    async def test_query_sketch_soft_miss_filter_field_partial(self):
        """未声明过滤字段 → 软 MISS 骨架,同样不拒绝。"""
        from trove.workflow.nodes.query_sketch import make_query_sketch

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        node = make_query_sketch(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"],
                "conditions": [{"field": "loan.ghost_col", "op": "=", "value": "x"}],
            })]),
            AgentConfig(target="mock/model"),
            semantic_layer=FakeProvider(self._demo_model()),
        )
        out = await node(make_state(question="?", matched_tables=["loan"]))
        assert "refusal" not in out
        assert out["compile_partial"] is True
        assert out["compile_meta"]["outcome"] == "partial"
        assert "unresolved_filter_field: loan.ghost_col" in out["plan"]

    async def test_query_sketch_hard_miss_still_refuses(self):
        """硬 MISS(结构性)仍拒绝——fan_out/基数不明/二义/未覆盖表不逃生。

        载体:关系声明了但**基数未声明**(反向 account→loan,to 侧非唯一键)
        → unknown_cardinality:many→one 无从判定,交 LLM 是赌安全,编译期拒。
        """
        from dataclasses import replace

        from trove.workflow.nodes.query_sketch import make_query_sketch

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        model = replace(
            self._demo_model(),
            relationships=[
                SemanticRelationship("account_to_loans", "account", "loan",
                                     from_columns=["account_id"],
                                     to_columns=["account_id"]),
            ],
        )
        node = make_query_sketch(
            ScriptedLLM([json.dumps({
                "tables": ["account"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model"),
            semantic_layer=FakeProvider(model),
        )
        out = await node(make_state(question="账户的平均贷款数?", matched_tables=["loan"]))
        assert out["refusal"] is not None
        assert out["compile_meta"]["outcome"] == "miss"
        assert out["compile_meta"]["miss_reason"] == "unknown_cardinality"
        assert out["compile_meta"]["miss_class"] == "hard"

    async def test_query_sketch_writes_compile_meta_both_paths(self):
        """编译决策观测:命中与 MISS 都写 compile_meta(eval hit-rate 闭环数据源)。"""
        from trove.workflow.nodes.query_sketch import make_query_sketch

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        # 命中路径
        node = make_query_sketch(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model"),
            semantic_layer=FakeProvider(self._demo_model()),
        )
        out = await node(make_state(question="how many loans?", matched_tables=["loan"]))
        assert out["compile_meta"]["outcome"] == "compiled"
        assert out["compile_meta"]["plan_typed"] is True
        assert out["compile_meta"]["semantic_layer"] is True
        assert out["compile_meta"]["miss_reason"] == ""

        # MISS 路径
        node_miss = make_query_sketch(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "sum(loan.ghost)",
                "answer_columns": ["sum(loan.ghost)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model"),
            semantic_layer=FakeProvider(self._demo_model()),
        )
        out_miss = await node_miss(
            make_state(question="贷款总额?", matched_tables=["loan"]))
        assert out_miss["compile_meta"]["outcome"] == "miss"
        assert out_miss["compile_meta"]["miss_reason"] == "no_metric_match"
        assert out_miss["compile_meta"]["miss_class"] == "soft"
        assert "sum(loan.ghost)" in out_miss["compile_meta"]["miss_component"]

    async def test_query_sketch_compile_meta_no_semantic_layer(self):
        """无语义层接线时:不编译,compile_meta 记 no_semantic_layer。

        ``no_plan_or_matched`` 属 HARD_MISS_REASONS —— 计划有意图时仍拒绝
        (图路由到 refuse 的 kb init 引导);本测试钉的是 compile_meta 的
        短路分因与拒绝分因的原样透出。
        """
        from trove.workflow.nodes.query_sketch import make_query_sketch

        node = make_query_sketch(
            ScriptedLLM([json.dumps({
                "tables": ["loan"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"],
                "conditions": [],
            })]),
            AgentConfig(target="mock/model"),
            semantic_layer=None,
        )
        out = await node(make_state(question="how many loans?", matched_tables=["loan"]))
        assert "compiled" not in out
        assert out["compile_meta"]["outcome"] == "miss"
        assert out["compile_meta"]["miss_reason"] == "no_semantic_layer"
        assert out["compile_meta"]["semantic_layer"] is False
        # 拒绝照旧(硬 MISS):分因是编译器原始值,不是 compile_meta 的短路值
        assert out["refusal"]["reason"] == "uncovered"
        assert out["refusal"]["compile_miss"]["reason"] == "no_plan_or_matched"

    async def test_compile_result_carries_its_source_plan(self):
        """A1-9:产物带回**编译它的那份计划**——引用同一性,不是长得一样的一份。

        用 ``is`` 断言:要的是同一个对象。P0-3 的教训正是"用这个 metric"≠
        "被校验过的那个 metric";编译产物能在交付时说出自己的来处,这条链
        才算接上。松 dict 入口(旧调用方/兜底)编译照常,来处为 None。
        """
        from trove.services.semantic_layer.plan import parse_plan_query
        from trove.workflow.nodes.query_sketch import _compile_semantic

        class FakeProvider:
            enabled = True

            def __init__(self, model):
                self._model = model

            def model(self):
                return self._model

        provider = FakeProvider(self._demo_model())
        raw_plan = {
            "tables": ["loan"],
            "aggregation": "count(loan.loan_id)",
            "answer_columns": ["count(loan.loan_id)"],
        }
        plan = parse_plan_query(raw_plan)
        assert plan is not None
        compiled, miss = _compile_semantic(plan, ["loan"], provider, "sqlite")
        assert miss is None and compiled is not None
        assert compiled.source_plan is plan

        # 松 dict 入参:编译产物一样,但"来处"是 None(不是经 IR 校验的计划)
        compiled_raw, miss_raw = _compile_semantic(raw_plan, ["loan"], provider, "sqlite")
        assert miss_raw is None and compiled_raw is not None
        assert compiled_raw.source_plan is None


class TestProgressiveSchemaLinking:
    """A3:反思轮放大候选——阈值放宽 + 上限提升,首轮行为不变。"""

    def test_progressive_threshold_and_limit(self):
        from trove.workflow.nodes.schema_linking import (
            _progressive_tables_limit,
            _progressive_threshold,
        )

        assert _progressive_threshold(0) == 2.0
        assert _progressive_threshold(1) == 1.5
        assert _progressive_threshold(2) == 1.0
        assert _progressive_threshold(9) == 1.0  # 封顶
        assert _progressive_tables_limit(0) == 8
        assert _progressive_tables_limit(1) == 12
        assert _progressive_tables_limit(2) == 16
        assert _progressive_tables_limit(9) == 16  # 封顶

    def test_retry_round_pulls_in_weaker_anchors(self):
        from trove.workflow.nodes.schema_linking import _semantic_match_datasets

        # 两库:loan(3.0 名称子串命中)→ trans(1.5 描述弱重叠,4 token 中 1 命中)。
        # 首轮阈值 2.0 只锚 loan;第 1 轮放宽到 1.5 把 trans 拉进来。
        model = SemanticModel(
            name="m",
            datasets=[
                SemanticDataset(name="loan", description="loan records for borrowers"),
                SemanticDataset(
                    name="trans", description="loan history ledger archive"),
            ],
        )
        q = "loan records"
        first = _semantic_match_datasets(model, q, [], None, retry_round=0)
        assert set(first) == {"loan"}
        expanded = _semantic_match_datasets(model, q, [], None, retry_round=1)
        assert set(expanded) == {"loan", "trans"}


class TestPlanContradictionReplan:
    """硬 MISS 二分与自愈:计划自相矛盾(模型里有、计划没带)且**可路由**
    → 编译器以组件引用为准自愈补齐,直接编译(B1);声明但**不可路由**
    (无关系边)→ 有界重规划;真模型缺口(未声明表)照旧拒绝。
    0483 形状自愈 / 0487 形状重规划 / 不可路由回归三条链路各自钉住。"""

    @staticmethod
    def _model() -> SemanticModel:
        f = lambda name: SemanticField(name=name, expression=name)  # noqa: E731
        return SemanticModel(
            name="fin",
            datasets=[
                SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                    f("loan_id"), f("client_id"), f("amount")]),
                SemanticDataset(name="client", primary_key=["client_id"], fields=[
                    f("client_id"), f("gender")]),
            ],
            relationships=[
                SemanticRelationship("loan_to_client", "loan", "client",
                                     from_columns=["client_id"],
                                     to_columns=["client_id"]),
            ],
            metrics=[
                SemanticMetric("number of loan records", "COUNT(loan.loan_id)",
                               datasets=["loan"]),
            ],
        )

    class _Provider:
        enabled = True

        def __init__(self, model):
            self._model = model

        def model(self):
            return self._model

    def _node(self, llm, model=None, **kwargs):
        from trove.workflow.nodes.query_sketch import make_query_sketch

        return make_query_sketch(
            llm, AgentConfig(target="mock/model"),
            semantic_layer=self._Provider(model or self._model()), **kwargs,
        )

    @staticmethod
    def _contradictory_plan() -> dict:
        """0483 形状:条件引用 client.gender,但 plan.tables 只带 loan。"""
        return {
            "tables": ["loan"],
            "aggregation": "count(loan.loan_id)",
            "answer_columns": ["count(loan.loan_id)"],
            "conditions": [{"field": "client.gender", "op": "=", "value": "F"}],
        }

    @staticmethod
    def _unroutable_model() -> SemanticModel:
        """同 _model 但**无关系边**:client 已声明却不可路由 —— 自愈把它补进
        join 集也 join 不到,仍是 unreachable_table。重规划链路的回归载体
        (自愈只吃「可唯一路由」的一类,不掩盖真缺口)。"""
        from dataclasses import replace

        return replace(TestPlanContradictionReplan._model(), relationships=[])

    async def test_declared_table_missing_from_plan_self_heals(self):
        """0483 形状(B1):条件引用 client、plan.tables 只带 loan,但关系已
        声明且可唯一路由 → 编译器以组件引用为准补齐 join 集,直接编译成功,
        不再是 unreachable_table,也不触发重规划。"""
        node = self._node(ScriptedLLM([json.dumps(self._contradictory_plan())]))
        out = await node(make_state(
            question="female clients' loans", matched_tables=["loan"]))
        assert "refusal" not in out
        assert out["compiled"] is True
        assert "JOIN client" in out["compiled_sql"]
        assert "client.gender = 'F'" in out["compiled_sql"]
        assert out["compile_meta"]["miss_reason"] == ""
        assert out["plan_replan_pending"] is False
        assert "error_feedback" not in out

    async def test_unroutable_declared_table_replans(self):
        """声明但不可路由(无关系边)→ 仍走有界重规划,不被自愈吞掉。"""
        node = self._node(ScriptedLLM([json.dumps(self._contradictory_plan())]),
                          model=self._unroutable_model())
        out = await node(make_state(
            question="female clients' loans", matched_tables=["loan"]))
        assert "refusal" not in out
        assert out["plan_replan_pending"] is True
        assert out["retry_count"] == 1
        assert out["plan_replan_rounds"] == 1
        assert out["error_feedback"].startswith("[ERR:PLAN_CONTRADICTION]")
        # 反馈必须携带字段清单,使 planner 能判断计划该带谁;无关系边时
        # 如实说明 no declared relationship,而不是编造 join。
        assert "no declared relationship" in out["error_feedback"]
        assert "gender" in out["error_feedback"]
        # 归因保留(miss 分因还看得见)……
        assert out["compile_meta"]["miss_reason"] == "unreachable_table"
        # ……但上一轮编译产物**必须清**:execute_sql 的保真校验读
        # compiled/compiled_sql,残留旧权威 SQL 会让下一条 SQL 被拿去和
        # 旧契约比对 → 假 COMPILE_DRIFT。
        assert out["compiled"] is False
        assert out["compiled_sql"] == ""
        assert out["compile_partial"] is False
        assert out["compile_misses"] == []
        assert out["contract"] is None

    async def test_replanned_plan_compiles(self):
        """重规划环的出口:计划修好 → 编译成功,信号复位,反馈进 planner。"""
        captured = {}

        class RecordingLLM:
            def __init__(self, responses):
                self._it = iter(responses)

            async def chat(self, model, messages, **kwargs):
                captured["prompt"] = " ".join(m["content"] for m in messages)
                return next(self._it)

        fixed = {
            "tables": ["loan", "client"],
            "aggregation": "count(loan.loan_id)",
            "answer_columns": ["count(loan.loan_id)"],
            "conditions": [{"field": "client.gender", "op": "=", "value": "F"}],
        }
        node = self._node(RecordingLLM([json.dumps(fixed)]))
        state = make_state(question="female clients' loans", matched_tables=["loan"])
        state.error_feedback = "[ERR:PLAN_CONTRADICTION] add client to plan.tables"
        state.plan_replan_rounds = 1
        out = await node(state)
        assert out["compiled"] is True
        assert "client.gender = 'F'" in out["compiled_sql"]
        assert out["plan_replan_pending"] is False
        assert captured["prompt"].find("PLAN_CONTRADICTION") != -1

    async def test_limit_without_order_replans(self):
        """0487 形状:limit 在、排序不可解析 → 同样是有界重规划。"""
        plan = {
            "tables": ["loan"],
            "aggregation": "count(loan.loan_id)",
            "answer_columns": ["count(loan.loan_id)"],
            "conditions": [],
            "limit": 10,
        }
        node = self._node(ScriptedLLM([json.dumps(plan)]))
        out = await node(make_state(question="top 10 loans", matched_tables=["loan"]))
        assert "refusal" not in out
        assert out["plan_replan_pending"] is True
        assert out["error_feedback"].startswith("[ERR:PLAN_CONTRADICTION]")
        assert "ordering" in out["error_feedback"]

    def test_whitelist_excludes_model_gaps(self):
        """白名单:真模型缺口与未知分因保持旧行为(照旧拒绝)。"""
        from trove.services.semantic_layer.compiler import CompileMiss
        from trove.workflow.nodes.query_sketch import _replan_feedback

        provider = self._Provider(self._model())
        plan = self._contradictory_plan()
        # 未声明表 → 模型缺口,不是计划缺陷
        assert _replan_feedback(
            plan, CompileMiss("unreachable_table", "tables outside join tree: ghost"),
            provider) is None
        # 白名单外分因(结构性硬 MISS 与软/未命中分因)一律不重规划
        assert _replan_feedback(plan, CompileMiss("fan_out", "loan, client"),
                                provider) is None
        assert _replan_feedback(plan, CompileMiss("unknown_cardinality", "loan"),
                                provider) is None
        assert _replan_feedback(plan, CompileMiss("no_metric_match", "x"),
                                provider) is None
        assert _replan_feedback(
            plan, CompileMiss("no_such_reason_ever", "x"), provider) is None

    def test_limit_without_order_feedback_lists_concrete_candidates(self):
        """0487:光说「补 ordering」重规划空转 —— 反馈必须带可写进 ordering
        的具体形态(相关声明度量名 + 计划已有的聚合列表达式)。"""
        from trove.services.semantic_layer.compiler import CompileMiss
        from trove.workflow.nodes.query_sketch import _replan_feedback

        plan = {
            "tables": ["loan"],
            "aggregation": "count(loan.loan_id)",
            "answer_columns": ["count(loan.loan_id)"],
            "conditions": [],
            "limit": 10,
        }
        text = _replan_feedback(
            plan, CompileMiss("limit_without_order", "ordering"),
            self._Provider(self._model()))
        assert text is not None
        assert text.startswith("[ERR:PLAN_CONTRADICTION]")
        assert "ordering" in text
        # 具体候选:声明度量名(loan 锚定)与计划里的聚合表达式
        assert "number of loan records" in text
        assert "count(loan.loan_id)" in text
        # 指令在前、≤600 字符(correction 通道有截断)
        assert len(text) <= 600

    def test_limit_without_order_without_semantic_layer_still_feedback(self):
        """无语义层也要给形态样例(度量名缺席,聚合表达式照给)。"""
        from trove.services.semantic_layer.compiler import CompileMiss
        from trove.workflow.nodes.query_sketch import _replan_feedback

        plan = {"tables": ["loan"], "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"], "limit": 5}
        text = _replan_feedback(
            plan, CompileMiss("limit_without_order", "ordering"), None)
        assert text is not None and "count(loan.loan_id)" in text

    def test_ambiguous_join_path_feedback_requires_full_replan(self):
        """二义 join 路径:反馈要求**整份**重计划 + 显式 plan.joins 官方路径,
        并附上声明的关系子句(只改被质疑的一段会改出第三条二义路径)。"""
        from trove.services.semantic_layer.compiler import CompileMiss
        from trove.workflow.nodes.query_sketch import _replan_feedback

        plan = {"tables": ["loan", "client"],
                "aggregation": "count(loan.loan_id)",
                "answer_columns": ["count(loan.loan_id)"], "conditions": []}
        text = _replan_feedback(
            plan, CompileMiss("ambiguous_join_path", "loan -> client"),
            self._Provider(self._model()))
        assert text is not None
        assert text.startswith("[ERR:PLAN_CONTRADICTION]")
        assert "whole" in text and "joins" in text
        assert "loan.client_id = client.client_id" in text
        assert len(text) <= 600

    def test_ambiguous_join_path_without_semantic_layer_is_none(self):
        """无 provider / 无模型:拿不到声明路径 → 不重规划(照旧拒绝)。"""
        from trove.services.semantic_layer.compiler import CompileMiss
        from trove.workflow.nodes.query_sketch import _replan_feedback

        plan = {"tables": ["loan", "client"], "aggregation": "count(loan.loan_id)"}
        assert _replan_feedback(
            plan, CompileMiss("ambiguous_join_path", "x"), None) is None

    async def test_replan_exhausted_falls_back_to_refusal(self):
        """双上限之一(MAX_PLAN_REPLANS):耗尽 → 拒绝,不是 error。
        载体=不可路由表:重规划修不了,额度用尽后归因仍是结构性硬 MISS。"""
        from trove.workflow.nodes.query_sketch import MAX_PLAN_REPLANS

        node = self._node(ScriptedLLM([json.dumps(self._contradictory_plan())]),
                          model=self._unroutable_model())
        out = await node(make_state(
            question="female clients' loans", matched_tables=["loan"],
            plan_replan_rounds=MAX_PLAN_REPLANS))
        assert out["refusal"] is not None
        assert out["refusal"]["compile_miss"]["reason"] == "unreachable_table"
        assert out["plan_replan_pending"] is False
        assert "error_feedback" not in out

    async def test_replan_budget_exhausted_falls_back_to_refusal(self):
        """双上限之二(共享修正预算):耗尽 → 拒绝,不是 error。"""
        node = self._node(ScriptedLLM([json.dumps(self._contradictory_plan())]),
                          model=self._unroutable_model())
        out = await node(make_state(
            question="female clients' loans", matched_tables=["loan"],
            retry_count=10))
        assert out["refusal"] is not None
        assert out["refusal"]["compile_miss"]["reason"] == "unreachable_table"
        assert out["plan_replan_pending"] is False
