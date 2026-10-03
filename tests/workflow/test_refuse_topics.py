"""主题域拒绝的确定性短路测试(零 LLM,零草稿写入)。

主题域类拒绝(域不存在 / 域过期 / 问题在域外)与"模型缺声明"的补救方式
不同:前者是换域或并域,后者是补建模。所以 refuse 节点必须在**进入草稿机
之前**分流 —— 否则既烧一次 LLM 起草一个与问题无关的 metric/field,又在
无域上下文时给出误导文案("模型缺少该声明"其实是"不在这个域里")。
"""
from __future__ import annotations

import pytest

from trove.core.config import AgentConfig
from trove.services.kb.service import KbService
from trove.workflow.nodes.refuse import make_refuse
from trove.workflow.state import WorkflowState


class ExplodingLLM:
    """任何调用都失败 —— 断言这几条路径**完全不碰 LLM**。"""

    async def chat(self, model, messages, **kwargs):
        raise AssertionError("topic refusal must not call the LLM")


@pytest.fixture
def kb(tmp_path):
    return KbService(tmp_path / "proj")


def _state(**kwargs) -> WorkflowState:
    defaults = {"session_id": "s1", "question": "客户性别分布", "lang": "zh"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


class TestTopicRefusalIsDeterministic:
    async def test_topic_not_found(self, kb):
        node = make_refuse(ExplodingLLM(), AgentConfig(target="mock/model"), kb=kb)
        out = await node(_state(datasource="demo", refusal={
            "reason": "topic_not_found", "topic": "ghost",
            "available_topics": ["loans", "clients"],
        }))
        assert out["refusal"]["reason"] == "topic_not_found"
        assert "ghost" in out["clarification_question"]
        assert "loans" in out["clarification_question"]  # 可选清单给出来
        assert out["refusal"]["available_topics"] == ["loans", "clients"]
        assert out["clarification_question"] == out["refusal"]["message"]

    async def test_topic_empty_scope(self, kb):
        node = make_refuse(ExplodingLLM(), AgentConfig(target="mock/model"), kb=kb)
        out = await node(_state(datasource="demo", refusal={
            "reason": "topic_empty_scope", "topic": "legacy",
            "available_topics": [],
        }))
        assert "legacy" in out["clarification_question"]
        assert "管理员" in out["clarification_question"]

    async def test_topic_out_of_scope_from_no_match(self, kb):
        """域内零锚定(no_semantic_match + topic)→ 域外文案,不是"缺声明"。"""
        node = make_refuse(ExplodingLLM(), AgentConfig(target="mock/model"), kb=kb)
        out = await node(_state(datasource="demo", refusal={
            "reason": "no_semantic_match", "topic": "loans",
            "topic_scope": ["loan", "account"],
            "available_topics": ["loans", "clients"],
        }))
        # reason 保持上游原值(机器匹配不变),文案按域外说
        assert out["refusal"]["reason"] == "no_semantic_match"
        assert "不在主题域「loans」的范围内" in out["clarification_question"]
        assert "loan、account" in out["clarification_question"]

    async def test_topic_refusal_writes_no_draft(self, kb):
        from trove.services.semantic_layer.manage import SemanticManager

        node = make_refuse(ExplodingLLM(), AgentConfig(target="mock/model"), kb=kb)
        await node(_state(datasource="demo", refusal={
            "reason": "topic_not_found", "topic": "ghost", "available_topics": []}))
        assert not SemanticManager(kb).drafts("demo")["pending"]

    async def test_no_topic_context_keeps_old_path(self, kb):
        """无 topic 键的 no_semantic_match 仍走原「未覆盖」路径(不误升级)。"""
        from tests.workflow.test_refuse import (  # noqa: F401  (复用脚本化 LLM 形态)
            METRIC_DRAFT_YAML, FakeProvider, ScriptedLLM, _demo_model,
        )

        node = make_refuse(
            ScriptedLLM([METRIC_DRAFT_YAML]),
            AgentConfig(target="mock/model"),
            kb=kb, semantic_layer=FakeProvider(_demo_model()),
        )
        out = await node(_state(datasource="demo", refusal={
            "reason": "no_semantic_match", "question": "平均贷款金额是多少?",
        }))
        assert out["refusal"]["conflict"] is False
        assert out["refusal"]["draft_entry"]["kind"] == "metric"
