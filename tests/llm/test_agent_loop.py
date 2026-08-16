"""Agent loop harness tests — model-driven termination with tool calls."""

import pytest

from trove.llm.agent_loop import run_agent_loop


class ScriptedLLM:
    """Responses: dict {"content": ..., "tool_calls": [...]} per chat_full call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat_full(self, model, messages, tools=None, **kwargs):
        # 快照：调用后 messages 列表还会被 loop 继续追加
        self.calls.append(list(messages))
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

    async def test_result_carries_compact_transcript(self):
        """轨迹回带:结果含紧凑思考痕迹(模型文本+工具调用+观测)。"""
        llm = ScriptedLLM([
            {"content": "先查一下表结构", "tool_calls": [
                {"id": "c1", "name": "echo", "arguments": '{"text": "hi"}'},
            ]},
            {"content": "final answer", "tool_calls": []},
        ])

        async def echo_handler(arguments: dict) -> str:
            return f"echoed: {arguments['text']}"

        result = await run_agent_loop(
            llm, "m", "sys", "user", TOOL_DEF, {"echo": echo_handler}, max_rounds=5,
        )
        transcript = result["transcript"]
        assert "先查一下表结构" in transcript
        assert "echo" in transcript
        assert "echoed: hi" in transcript

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

    async def test_guard_returns_last_model_content(self):
        """护栏命中时返回模型最后一轮的非空文本，而不是空串。

        DeepSeek 常在同一条回复里既给 content 又给 tool_calls；
        若只在不调工具的那轮才记录 content，护栏命中会丢掉模型最后说过的话。
        """
        responses = [
            {"content": "thinking 1", "tool_calls": [
                {"id": "c1", "name": "echo", "arguments": '{"text": "x"}'},
            ]},
            {"content": "thinking 2", "tool_calls": [
                {"id": "c2", "name": "echo", "arguments": '{"text": "y"}'},
            ]},
            {"content": "RETRY: 数值不对", "tool_calls": [
                {"id": "c3", "name": "echo", "arguments": '{"text": "z"}'},
            ]},
        ]

        async def echo_handler(arguments: dict) -> str:
            return "ok"

        result = await run_agent_loop(
            llm=ScriptedLLM(list(responses)), model="m", system="sys", user="user",
            tools=TOOL_DEF, tool_handlers={"echo": echo_handler}, max_rounds=3,
        )
        assert result["guard_hit"] is True
        assert result["content"] == "RETRY: 数值不对"

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
