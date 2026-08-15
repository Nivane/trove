"""Agent loop harness tests — model-driven termination with tool calls."""

import pytest

from trove.llm.agent_loop import run_agent_loop


class ScriptedLLM:
    """Responses: dict {"content": ..., "tool_calls": [...]} per chat_full call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return self._responses.pop(0)


TOOL_DEF = [{
    "type": "function",
    "function": {"name": "echo", "description": "echo", "parameters": {}},
}]


class TestAgentLoop:
    async def test_single_tool_round_then_finish(self):
        """工具调用 → 观测 → 模型不再调用 → 终止（模型自主决定）。"""
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "echo", "arguments": '{"text": "hi"}'},
            ]},
            {"content": "final answer", "tool_calls": []},
        ])

        async def echo_handler(arguments: dict) -> str:
            return f"echoed: {arguments['text']}"

        result = await run_agent_loop(
            llm, "m", "sys", "user", TOOL_DEF, {"echo": echo_handler}, max_rounds=5,
        )
        assert result["content"] == "final answer"
        assert result["rounds"] == 2
        assert result["tool_history"][0]["name"] == "echo"
        assert result["tool_history"][0]["arguments"] == {"text": "hi"}
        # 工具结果作为 observation 回传给了模型
        tool_message = llm.calls[1][-1]
        assert tool_message["role"] == "tool"
        assert "echoed: hi" in tool_message["content"]

    async def test_unknown_tool_is_observation(self):
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [{"id": "c1", "name": "ghost", "arguments": "{}"}]},
            {"content": "done", "tool_calls": []},
        ])
        result = await run_agent_loop(llm, "m", "sys", "user", TOOL_DEF, {}, max_rounds=5)
        assert result["content"] == "done"
        assert "Unknown tool" in llm.calls[1][-1]["content"]

    async def test_max_rounds_guard(self):
        """护栏：模型一直调工具 → 达到 max_rounds 强制终止。"""
        responses = [
            {"content": None, "tool_calls": [{"id": "c1", "name": "echo", "arguments": '{"text": "x"}'}]},
        ] * 10
        llm = ScriptedLLM(responses)

        async def echo_handler(arguments: dict) -> str:
            return "ok"

        result = await run_agent_loop(llm, "m", "sys", "user", TOOL_DEF, {"echo": echo_handler}, max_rounds=3)
        assert result["rounds"] == 3
        assert result["guard_hit"] is True

    async def test_tool_handler_failure_is_observation(self):
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [{"id": "c1", "name": "echo", "arguments": "{}"}]},
            {"content": "recovered", "tool_calls": []},
        ])

        async def broken(arguments: dict) -> str:
            raise RuntimeError("boom")

        result = await run_agent_loop(llm, "m", "sys", "user", TOOL_DEF, {"echo": broken}, max_rounds=5)
        assert result["content"] == "recovered"
        assert "boom" in llm.calls[1][-1]["content"]
