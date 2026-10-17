"""gen 链的出境治理接线:装配阶段脱敏样例值 + 生成阶段汇总观测报告。

引擎行为在 ``tests/services/authz/test_egress.py``,注册表接线在
``tests/llm/test_agent_loop_egress.py``;这里钉的是 ``graphs`` 的装配线:

- ``gen_assemble`` 在预算装配与 prompt 渲染**之前**替换 few-shot 样例值;
- 未声明部署不产生 ``egress_report`` 键(逐字节不变);
- ``gen_generate`` 的注册表报告并入 state(整段 run 的汇总);
- redactor 缺失时按语义层 + 活配置懒构造并复用。
"""

from __future__ import annotations

from trove.core.config import AgentConfig, MaskingConfig
from trove.services.authz.egress import EgressRedactor
from trove.services.semantic_layer.models import (
    MaskingPolicy,
    SemanticDataset,
    SemanticField,
    SemanticModel,
)
from trove.workflow import graphs as graphs_module
from trove.workflow.graphs import _egress_redactor, build_gen_sql_subgraph

from tests.workflow.test_graphs import AgenticLLM, make_services, make_state


def _masked_model() -> SemanticModel:
    return SemanticModel(
        datasets=[SemanticDataset(
            name="customers", source="public.customers",
            fields=[
                SemanticField(name="phone", expression="phone", mask="partial"),
                SemanticField(name="city", expression="city"),
            ],
        )],
        masking=MaskingPolicy(),
    )


class _Provider:
    def __init__(self, model: SemanticModel):
        self._model = model

    def model(self) -> SemanticModel:
        return self._model


def _config() -> AgentConfig:
    cfg = AgentConfig(target="mock/model")
    cfg.masking = MaskingConfig(hash_salt_ref="")
    return cfg


def _gen_ctx(few_shots: list[dict]) -> dict:
    return {
        "dialect": "sqlite",
        "complexity": "standard",
        "in_correction": False,
        "few_shots": few_shots,
        "term_notes": [],
        "lessons": [],
        "rules": [],
        "metrics": [],
        "entities": [],
        "user_facts": [],
        "episodes": [],
        "profile": "",
        # 装配按 zip(example_hits, few_shots) 配对(检索侧同源两条列表)
        "examples": [
            {"question": s.get("question", ""), "sql": s.get("sql", ""), "score": 0.9}
            for s in few_shots
        ],
        "kb_exact_match": None,
    }


SHOTS = [{
    "question": "find customer by phone",
    "sql": "SELECT * FROM customers WHERE phone = '{{phone}}' AND city = '{{city}}'",
    "parameters": [
        {"name": "phone", "type": "dimension", "column": "customers.phone",
         "sample_values": ["13812348888"]},
        {"name": "city", "type": "dimension", "column": "customers.city",
         "sample_values": ["sh"]},
    ],
}]


class FakeRedactor:
    """记录调用的 redactor(接线测试不重复覆盖掩码引擎)。"""

    def __init__(self):
        self.tool_calls: list[str] = []

    def redact_tool_result(self, name, args, value):
        self.tool_calls.append(name)
        return "REDACTED-OBS", {
            "channel": "tool_result", "tool": name,
            "fields": {"name": "partial"}, "rows": 1, "values": 0,
        }

    def redact_shots(self, shots):
        return shots, {}


class TestAssemble:
    async def test_sample_values_redacted_before_assembly(self):
        services = make_services(AgenticLLM([]), config=_config())
        services.egress = EgressRedactor(_Provider(_masked_model()), lambda: services.config)
        state = make_state(
            gen_ctx=_gen_ctx(SHOTS), schema_context="CREATE TABLE customers (phone TEXT)",
        )
        out = await graphs_module.make_gen_assemble(services)(state)
        shots = out["gen_sub_state"]["few_shots"]
        assert shots[0]["parameters"][0]["sample_values"] == ["138****8888"]
        # 未声明字段原样
        assert shots[0]["parameters"][1]["sample_values"] == ["sh"]
        assert out["egress_report"]["prompt"] == {
            "fields": {"phone": "partial"}, "rows": 0, "values": 1,
        }
        # 入参(gen_ctx 里的检索结果)不被改写
        assert SHOTS[0]["parameters"][0]["sample_values"] == ["13812348888"]

    async def test_no_declarations_leaves_no_report_key(self):
        """无语义层 = 没有声明面:不产生 egress_report(老路径逐字节不变)。"""
        services = make_services(AgenticLLM([]), config=_config())
        state = make_state(
            gen_ctx=_gen_ctx(SHOTS), schema_context="CREATE TABLE customers (phone TEXT)",
        )
        out = await graphs_module.make_gen_assemble(services)(state)
        assert "egress_report" not in out
        assert out["gen_sub_state"]["few_shots"][0]["parameters"][0][
            "sample_values"] == ["13812348888"]

    async def test_redactor_is_built_once_and_cached(self):
        services = make_services(AgenticLLM([]), config=_config())
        r1 = _egress_redactor(services)
        assert isinstance(r1, EgressRedactor)
        assert _egress_redactor(services) is r1
        # 显式注入的实例优先(测试/嵌入方替换实现)
        injected = FakeRedactor()
        services.egress = injected
        assert _egress_redactor(services) is injected


class TestGenerateReport:
    async def test_probe_observation_redacted_and_report_merged(
        self, sqlite_registry, catalog,
    ):
        """整条 gen 链:probe 回喂被脱敏,报告并入 update.egress_report。"""
        llm = AgenticLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "probe_query",
                 "arguments": '{"sql": "SELECT name FROM students"}'},
            ]},
            {"content": "```sql\nSELECT name FROM students;\n```", "tool_calls": []},
        ])
        services = make_services(llm, catalog, sqlite_registry)
        redactor = FakeRedactor()
        services.egress = redactor
        sub = build_gen_sql_subgraph(services)

        state = make_state(question="list all student names", matched_tables=["students"])
        r1 = await graphs_module.make_gen_retrieve(services)(state)
        r2 = await graphs_module.make_gen_assemble(services)(
            state.model_copy(update=r1))
        r3 = await graphs_module.make_gen_generate(services, sub, agentic=True)(
            state.model_copy(update={**r1, **r2}))

        assert "probe_query" in redactor.tool_calls
        tool_msgs = [
            m for msgs in llm.calls for m in msgs if m.get("role") == "tool"
        ]
        assert any("REDACTED-OBS" in m["content"] for m in tool_msgs)
        assert all("Alice" not in str(m["content"]) for m in tool_msgs)
        assert r3["egress_report"]["probe_query"] == {
            "fields": {"name": "partial"}, "rows": 1, "values": 0,
        }
        # SQL 定稿不受影响(控制值不走脱敏)
        assert r3["sql"] == "SELECT name FROM students;"
