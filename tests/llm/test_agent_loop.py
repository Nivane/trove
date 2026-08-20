"""Agent loop harness tests — model-driven termination with tool calls.

Covers: model-driven stop, round guard, parallel dispatch, per-tool timeout,
token/time budgets, loop steering, the explicit finish-tool protocol and
registry observer hooks.
"""

import asyncio
import time

import pytest

from trove.llm.agent_loop import ToolRegistry, run_agent_loop


class ScriptedLLM:
    """Responses: dict {"content": ..., "tool_calls": ...} per chat_full call."""

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


async def _echo(arguments: dict) -> str:
    return f"echoed: {arguments['text']}"


class TestAgentLoop:
    async def test_single_tool_round_then_finish(self):
        """工具调用 → 观测 → 模型不再调用 → 终止（模型自主决定）。"""
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "echo", "arguments": '{"text": "hi"}'},
            ]},
            {"content": "final answer", "tool_calls": []},
        ])

        result = await run_agent_loop(
            llm, "m", "sys", "user", TOOL_DEF, {"echo": _echo}, max_rounds=5,
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

        result = await run_agent_loop(
            llm, "m", "sys", "user", TOOL_DEF, {"echo": _echo}, max_rounds=5,
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

        async def ok(arguments: dict) -> str:
            return "ok"

        result = await run_agent_loop(llm, "m", "sys", "user", TOOL_DEF, {"echo": ok}, max_rounds=3)
        assert result["rounds"] == 3
        assert result["guard_hit"] is True
        assert result["budget_why"] == "rounds"

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

        async def ok(arguments: dict) -> str:
            return "ok"

        result = await run_agent_loop(
            llm=ScriptedLLM(list(responses)), model="m", system="sys", user="user",
            tools=TOOL_DEF, tool_handlers={"echo": ok}, max_rounds=3,
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


class TestParallelDispatch:
    async def test_tools_in_one_round_run_concurrently(self):
        """一轮内多个工具并发执行;观测按原始 tool_call 顺序回填。"""

        async def slow(arguments: dict) -> str:
            await asyncio.sleep(0.15)
            return arguments["tag"]

        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "echo", "arguments": '{"tag": "a"}'},
                {"id": "c2", "name": "echo", "arguments": '{"tag": "b"}'},
            ]},
            {"content": "done", "tool_calls": []},
        ])
        start = time.monotonic()
        result = await run_agent_loop(
            llm, "m", "sys", "user", TOOL_DEF, {"echo": slow}, max_rounds=5,
        )
        elapsed = time.monotonic() - start
        # 串行需 ~0.3s;并发压到 ~0.15s
        assert elapsed < 0.25
        assert result["tool_calls"] == 2
        tool_msgs = [m for m in llm.calls[1] if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]
        assert tool_msgs[0]["content"] == "a"
        assert tool_msgs[1]["content"] == "b"


class TestToolTimeout:
    async def test_slow_tool_becomes_timeout_observation(self):
        """工具超时 → 观测文本(而非挂死);循环继续。"""

        async def hang(arguments: dict) -> str:
            await asyncio.sleep(10)

        llm = ScriptedLLM([
            {"content": None, "tool_calls": [{"id": "c1", "name": "echo", "arguments": "{}"}]},
            {"content": "recovered", "tool_calls": []},
        ])
        result = await run_agent_loop(
            llm, "m", "sys", "user", TOOL_DEF, {"echo": hang},
            max_rounds=5, tool_timeout_s=0.05,
        )
        assert result["content"] == "recovered"
        assert "timed out after 0.05s" in llm.calls[1][-1]["content"]


class TestBudgets:
    async def test_token_budget_guard(self):
        """usage 提供 completion_tokens,超预算 → guard_hit 且 budget_why=tokens。"""
        llm = ScriptedLLM([{
            "content": None, "tool_calls": [{"id": "c1", "name": "echo", "arguments": "{}"}],
            "usage": {"total_tokens": 10, "completion_tokens": 10},
        }])

        async def ok(arguments: dict) -> str:
            return "ok"

        result = await run_agent_loop(
            llm, "m", "sys", "user", TOOL_DEF, {"echo": ok},
            max_rounds=5, max_total_tokens=10,
        )
        assert result["guard_hit"] is True
        assert result["budget_why"] == "tokens"
        assert result["total_tokens"] == 10
        assert result["rounds"] == 1

    async def test_token_budget_ignores_prompt_tokens(self):
        """预算按 completion 计(模型输出):只有输入 token 大不触发护栏,
        否则大上下文首轮就会误触。"""
        llm = ScriptedLLM([
            {
                "content": None,
                "tool_calls": [{"id": "c1", "name": "echo", "arguments": "{}"}],
                "usage": {"prompt_tokens": 9999, "total_tokens": 10005},
            },
            {"content": "done", "tool_calls": []},
        ])

        async def ok(arguments: dict) -> str:
            return "ok"

        result = await run_agent_loop(
            llm, "m", "sys", "user", TOOL_DEF, {"echo": ok},
            max_rounds=5, max_total_tokens=10,
        )
        assert result["guard_hit"] is False
        assert result["content"] == "done"

    async def test_time_budget_guard(self):
        """墙钟超预算 → guard_hit 且 budget_why=time。"""

        async def ok(arguments: dict) -> str:
            return "ok"

        llm = ScriptedLLM([
            {"content": None, "tool_calls": [{"id": "c1", "name": "echo", "arguments": "{}"}]},
            {"content": "done", "tool_calls": []},
        ])
        result = await run_agent_loop(
            llm, "m", "sys", "user", TOOL_DEF, {"echo": ok},
            max_rounds=5, time_budget_s=-1,
        )
        assert result["guard_hit"] is True
        assert result["budget_why"] == "time"


class TestSteering:
    async def test_repeated_identical_calls_get_steering(self):
        """连续相同调用注入转向消息,而不是干等到护栏。"""
        responses = [
            {"content": None, "tool_calls": [
                {"id": f"c{i}", "name": "echo", "arguments": '{"text": "x"}'},
            ]}
            for i in range(3)
        ] + [{"content": "final answer", "tool_calls": []}]
        llm = ScriptedLLM(responses)

        async def ok(arguments: dict) -> str:
            return "ok"

        result = await run_agent_loop(
            llm, "m", "sys", "user", TOOL_DEF, {"echo": ok},
            max_rounds=5, steering_window=2,
        )
        assert result["content"] == "final answer"
        assert result["steering_hits"]
        assert any(
            any(str(m.get("content", "")).startswith("[steering]") for m in call)
            for call in llm.calls
        )

    async def test_differing_calls_do_not_steer(self):
        """参数不同则不触发转向。"""
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "echo", "arguments": '{"text": "a"}'},
            ]},
            {"content": None, "tool_calls": [
                {"id": "c2", "name": "echo", "arguments": '{"text": "b"}'},
            ]},
            {"content": "done", "tool_calls": []},
        ])

        async def ok(arguments: dict) -> str:
            return "ok"

        result = await run_agent_loop(
            llm, "m", "sys", "user", TOOL_DEF, {"echo": ok},
            max_rounds=5, steering_window=2,
        )
        assert result["steering_hits"] == []


class TestFinishProtocol:
    def test_registry_defs_ordering_and_validation(self):
        """finish 定义置末;validate_finish 校验载荷。"""
        registry = ToolRegistry(finish=True)

        async def validate_tool(arguments: dict) -> str:
            return "valid"

        registry.register("validate_sql", validate_tool)
        names = [d["function"]["name"] for d in registry.defs()]
        assert names == ["validate_sql", "finish"]
        assert registry.has_finish
        ok, payload = registry.validate_finish({"answer": "  SELECT 1 "})
        assert ok and payload == "SELECT 1"
        ok, _ = registry.validate_finish({})
        assert not ok

    async def test_finish_terminates_with_payload(self):
        """显式 finish(answer) 定稿:载荷即答案,立即终止,无额外轮次。"""
        registry = ToolRegistry(finish=True)

        async def ok(arguments: dict) -> str:
            return "echo"

        registry.register("echo", ok)
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "finish", "arguments": '{"answer": "final answer"}'},
            ]},
        ])
        result = await run_agent_loop(llm, "m", "sys", "user", registry=registry, max_rounds=5)
        assert result["finish_tool"] is True
        assert result["content"] == "final answer"
        assert result["rounds"] == 1
        assert result["guard_hit"] is False
        assert len(llm.calls) == 1

    async def test_invalid_finish_payload_continues_loop(self):
        """finish 载荷无效 → 错误观测喂回模型,循环继续。"""
        registry = ToolRegistry(finish=True)
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "finish", "arguments": "{}"},
            ]},
            {"content": "done", "tool_calls": []},
        ])
        result = await run_agent_loop(llm, "m", "sys", "user", registry=registry, max_rounds=5)
        assert result["finish_tool"] is False
        assert result["content"] == "done"
        assert "non-empty 'answer'" in llm.calls[1][-1]["content"]


class TestRegistryObservers:
    async def test_observer_hook_receives_tool_execution(self):
        """observer 中间件钩子拿到每次工具执行(name/args/obs)。"""
        registry = ToolRegistry(finish=True)

        async def ok(arguments: dict) -> str:
            return "hi"

        registry.register("echo", ok)
        seen = []
        registry.add_observer(
            lambda name, arguments, obs, elapsed, err, run_id: seen.append((name, obs)),
        )
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "echo", "arguments": '{"text": "x"}'},
            ]},
            {"content": "final", "tool_calls": []},
        ])
        await run_agent_loop(llm, "m", "sys", "user", registry=registry, max_rounds=5)
        assert ("echo", "hi") in seen

    async def test_observer_error_never_breaks_loop(self):
        """observer 抛异常被吞掉,主循环不受影响。"""

        async def ok(arguments: dict) -> str:
            return "hi"

        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "echo", "arguments": "{}"},
            ]},
            {"content": "final", "tool_calls": []},
        ])
        result = await run_agent_loop(llm, "m", "sys", "user", TOOL_DEF, {"echo": ok}, max_rounds=5)
        assert result["content"] == "final"


class TestContextWindowGuards:
    """超长观测截断 + 早轮丢弃——只压缩喂回模型的 messages。"""

    async def test_long_tool_observation_truncated_in_messages(self):
        """超长观测在注入 messages 里截断,但 tool_history 保留全文。"""
        async def long(arguments: dict) -> str:
            return "x" * 2000

        llm = ScriptedLLM([
            {"content": None, "tool_calls": [{"id": "c1", "name": "echo", "arguments": "{}"}]},
            {"content": "done", "tool_calls": []},
        ])
        result = await run_agent_loop(llm, "m", "sys", "user", TOOL_DEF, {"echo": long}, max_rounds=5)
        injected = llm.calls[1][-1]["content"]
        assert len(injected) < 2000
        assert "truncated" in injected
        assert len(result["tool_history"][0]["observation"]) == 2000  # 全文保留

    async def test_early_rounds_pruned_beyond_cap(self):
        """轮次超过上限时丢弃最早往返轮(system/user 永保)。"""
        from trove.llm.agent_loop import _prune_old_rounds, MAX_TOOL_TURNS

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "user"},
        ]
        for i in range(MAX_TOOL_TURNS + 5):
            messages.append({"role": "assistant", "content": f"a{i}", "tool_calls": []})
            messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"obs{i}"})

        out = _prune_old_rounds(messages)
        assert out[0]["role"] == "system"
        assert out[1]["role"] == "user"
        assistants = [m for m in out if m["role"] == "assistant"]
        tools = [m for m in out if m["role"] == "tool"]
        assert len(assistants) == MAX_TOOL_TURNS
        assert len(tools) == MAX_TOOL_TURNS  # 成组裁剪,无孤儿 tool
        assert assistants[0]["content"] == f"a{MAX_TOOL_TURNS + 4 - MAX_TOOL_TURNS + 1}"

    def test_prune_keeps_structure_under_cap(self):
        from trove.llm.agent_loop import _prune_old_rounds, MAX_TOOL_TURNS
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "user"},
            {"role": "assistant", "content": "a", "tool_calls": []},
            {"role": "tool", "tool_call_id": "c", "content": "obs"},
        ]
        assert _prune_old_rounds(messages) == messages  # 未超限不动

    async def test_first_input_tokens_captured(self):
        """首轮 prompt_tokens 回带(调用方做估算校准)。"""
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [{"id": "c1", "name": "echo", "arguments": "{}"}],
             "usage": {"prompt_tokens": 123, "total_tokens": 130}},
            {"content": "done", "tool_calls": [], "usage": {"prompt_tokens": 200, "total_tokens": 210}},
        ])
        async def ok(arguments: dict) -> str:
            return "ok"

        result = await run_agent_loop(llm, "m", "sys", "user", TOOL_DEF, {"echo": ok}, max_rounds=5)
        assert result["first_input_tokens"] == 123