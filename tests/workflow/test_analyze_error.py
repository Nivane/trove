"""Error analysis node tests — classify, judge, and plan the fix."""

import pytest

from trove.core.config import AgentConfig
from trove.workflow.nodes.analyze_error import make_analyze_error
from trove.workflow.state import WorkflowState


def make_state(**kwargs):
    defaults = {"session_id": "s1", "question": "q"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


class TestAnalyzeError:
    async def test_produces_analysis_with_classification(self):
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(messages=messages, **kwargs)
                return "类型: Schema Linking\n判断: loans 表不存在\n修正: 改用 loan"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        state = make_state(
            sql="SELECT * FROM loans",
            error_feedback="no such table: loans",
            schema_context="Table: loan",
        )
        update = await node(state)
        assert "Schema Linking" in update["error_analysis"]
        # prompt 含错误 SQL、错误信息、schema 与分类要求
        prompt = " ".join(m["content"] for m in captured["messages"])
        assert "loans" in prompt
        assert "no such table" in prompt
        assert "Schema Linking" in prompt

    async def test_no_feedback_passes(self):
        class NoLLM:
            async def chat(self, *a, **k):
                raise AssertionError("must not run")

        node = make_analyze_error(NoLLM(), AgentConfig(target="m"))
        assert await node(make_state()) == {}

    async def test_prompt_includes_evidence_hint(self):
        """诊断必须看到官方 evidence,不能凭记忆误判业务语义。"""
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(messages=messages, **kwargs)
                return "TARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        state = make_state(
            sql="SELECT 1",
            error_feedback="results differ",
            evidence="'POPLATEK PO OBRATU' represents for 'issuance after transaction'",
        )
        await node(state)
        prompt = " ".join(m["content"] for m in captured["messages"])
        assert "POPLATEK PO OBRATU" in prompt

    async def test_prompt_includes_prior_reasoning_trail(self):
        """诊断必须看到上一轮生成/规划方的思考轨迹,才能定位误判根因。"""
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured.update(messages=messages, **kwargs)
                return "TARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        state = make_state(
            sql="SELECT 1",
            error_feedback="results differ",
            reasoning_history=[
                {"node": "reflect", "text": "无关节点的痕迹不应出现"},
                {"node": "gen_sql", "text": "先试 validate_sql 再决定用哪个解释"},
            ],
        )
        await node(state)
        prompt = " ".join(m["content"] for m in captured["messages"])
        assert "validate_sql" in prompt          # gen_sql 的轨迹进 prompt
        assert "无关节点" not in prompt           # 其他节点的痕迹不混入

    async def test_llm_failure_is_silent(self):
        class BrokenLLM:
            async def chat(self, *a, **k):
                raise RuntimeError("down")

        node = make_analyze_error(BrokenLLM(), AgentConfig(target="m"))
        assert await node(make_state(error_feedback="boom")) == {}

    async def test_empty_output_gets_deterministic_fallback(self):
        """LLM 调用成功但输出为空 → 按错误模式给出确定性兜底诊断,不静默放行。"""
        class EmptyLLM:
            async def chat(self, model, messages, **kwargs):
                return ""

        node = make_analyze_error(EmptyLLM(), AgentConfig(target="m"))
        update = await node(make_state(
            error_feedback="Validation rule: list question returned no rows",
            lang="en",
        ))
        assert "empty result" in update["error_analysis"].lower()
        assert "filter" in update["error_analysis"].lower()
        assert update["rollback_target"] == "gen_sql"

    async def test_empty_output_fallback_syntax_pattern(self):
        class EmptyLLM:
            async def chat(self, model, messages, **kwargs):
                return ""

        node = make_analyze_error(EmptyLLM(), AgentConfig(target="m"))
        update = await node(make_state(
            error_feedback="MySQL execution error: (1064, syntax error",
            lang="en",
        ))
        assert "syntax" in update["error_analysis"].lower()

    async def test_no_sql_verdict(self):
        """诊断判定问题本身不是 SQL 问题 → no_sql 出口 + 清掉陈旧反馈。"""
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "NO_SQL: 这不是数据查询，是表含义问题"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            error_feedback="Candidate SQL variants returned different results (1 vs 10 rows)",
        ))
        assert update["no_sql"] is True
        assert update["error_feedback"] == ""  # 避免陈旧反馈注入答案 prompt / 跳过裁决
        assert "NO_SQL" in update["error_analysis"]  # 轨迹展示保留诊断文本


class TestRollbackDecision:
    """LLM 判断回退目标：解析、默认值、防打转护栏、RETRY 触发。"""

    async def test_parses_target_from_response(self):
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "类型: 计划偏差\n判断: 分组维度错了\n修正: 按 county 分组\nTARGET: planner"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            sql="SELECT AVG(grade) FROM students",
            error_feedback="wrong grouping",
        ))
        assert update["rollback_target"] == "planner"
        assert update["last_rollback_target"] == "planner"

    async def test_missing_target_defaults_to_gen_sql(self):
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "diag: 表名错误"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(error_feedback="boom"))
        assert update["rollback_target"] == "gen_sql"

    async def test_repeated_target_escalates(self):
        """同一回退目标连续两次仍失败 → 强制升一档。"""
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "TARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            error_feedback="boom",
            last_rollback_target="gen_sql",
        ))
        assert update["rollback_target"] == "planner"  # gen_sql → planner 升级

    async def test_repeated_last_target_degrades(self):
        """护栏顶部目标仍重复 → 优雅降级（state.error）。"""
        class LLM:
            async def chat(self, model, messages, **kwargs):
                return "TARGET: schema_linking"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            error_feedback="boom",
            last_rollback_target="schema_linking",
        ))
        assert update["error"]

    async def test_runs_on_reflect_retry(self):
        """reflect RETRY（无 error_feedback）也进诊断，reason 作为失败上下文。"""
        captured = {}

        class LLM:
            async def chat(self, model, messages, **kwargs):
                captured["prompt"] = " ".join(m["content"] for m in messages)
                return "类型: 业务语义\n判断: 分组错\n修正: 按 county 分组\nTARGET: gen_sql"

        node = make_analyze_error(LLM(), AgentConfig(target="m"))
        update = await node(make_state(
            sql="SELECT AVG(grade) FROM students",
            verdict="RETRY",
            reason="wrong grouping",
        ))
        assert "wrong grouping" in captured["prompt"]
        assert update["rollback_target"] == "gen_sql"
