"""体检用到的三处生产抽取 —— 默认路径必须与抽取前逐字节一致。

- ``resolve_sketch_model``:preflight 展示的模型 = 节点真正调用的模型;
- ``make_query_sketch(schema_map=...)``:体检一次 run 只读一次 schema,
  但**不传**时必须仍按老路径现取(生产行为不变);
- 语义层构造抽取见 ``tests/services/semantic_layer/test_factory.py``。
"""

from __future__ import annotations

from trove.core.config import AgentConfig
from trove.workflow.nodes.query_sketch import (
    make_query_sketch,
    resolve_sketch_model,
)
from trove.workflow.state import WorkflowState


class _LLM:
    def __init__(self, response: str = "plan text"):
        self._response = response
        self.models: list[str] = []

    async def chat(self, model, messages, **kwargs):
        self.models.append(model)
        return self._response


class _Connectors:
    def __init__(self):
        self.schema_calls = 0

    async def get_schema(self, datasource=None):
        self.schema_calls += 1
        from trove.core.types import ColumnInfo, SchemaInfo, TableInfo

        return SchemaInfo(tables=[TableInfo(name="loan", columns=[
            ColumnInfo(name="loan_id", type="integer")])])


def _state(**kw) -> WorkflowState:
    base = dict(session_id="s1", question="how many loans", datasource="demo")
    base.update(kw)
    return WorkflowState(**base)


# ── resolve_sketch_model ────────────────────────────────────────────────


def test_resolve_sketch_model_precedence_and_source():
    assert resolve_sketch_model(AgentConfig(
        target="target/m", model_fast="fast/m",
        node_models={"query_sketch": "node/m"})) == (
            "node/m", "node_models.query_sketch")
    assert resolve_sketch_model(AgentConfig(
        target="target/m", model_fast="fast/m")) == ("fast/m", "model_fast")
    assert resolve_sketch_model(AgentConfig(target="target/m")) == (
        "target/m", "target")
    assert resolve_sketch_model(AgentConfig()) == ("openai/gpt-4o", "default")


def test_resolve_sketch_model_ignores_empty_node_override():
    # 空串 = 未覆盖(与节点原来的 ``or`` 链同义),不是「模型名是空串」。
    assert resolve_sketch_model(AgentConfig(
        target="t", node_models={"query_sketch": ""})) == ("t", "target")


async def test_node_uses_the_resolved_model():
    config = AgentConfig(target="target/m", model_fast="fast/m",
                         node_models={"query_sketch": "node/m"})
    llm = _LLM()
    node = make_query_sketch(llm, config)
    await node(_state())
    assert llm.models == ["node/m"]


# ── schema_map 注入 ─────────────────────────────────────────────────────


async def test_schema_map_injection_skips_live_schema_read():
    connectors = _Connectors()
    llm = _LLM()
    node = make_query_sketch(
        llm, AgentConfig(target="t"), connectors=connectors,
        schema_map={"loan": {"loan_id"}})
    update = await node(_state())
    assert connectors.schema_calls == 0
    assert update["plan"] == "plan text"


async def test_empty_schema_map_is_a_legal_value():
    # 空 dict 是「空库」,不是「没传」—— 传了就不该再现场读 schema。
    connectors = _Connectors()
    node = make_query_sketch(
        _LLM(), AgentConfig(target="t"), connectors=connectors, schema_map={})
    await node(_state())
    assert connectors.schema_calls == 0


async def test_default_path_still_reads_schema():
    # 不传 = 生产老路径:取一次 live schema。
    connectors = _Connectors()
    node = make_query_sketch(
        _LLM(), AgentConfig(target="t"), connectors=connectors)
    await node(_state())
    assert connectors.schema_calls == 1
