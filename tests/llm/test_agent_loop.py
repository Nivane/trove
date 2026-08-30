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


def _check_registry():
    """minimal registry with a check_result handler mimicking gen_sql's contract:
    pass → "OK (N rows)", violation → "VIOLATION [rule] reason"."""
    registry = ToolRegistry(finish=True)

    async def check_result(arguments: dict) -> str:
        sql = arguments.get("sql", "")
        if "bad" in sql:
            return "VIOLATION [F2] missing filter condition"
        return "OK (3 rows)"

    registry.register("check_result", check_result)
    return registry


class TestAutoFinishOnCheckOk:
    """check_result 通过 → harness 自动按 finish 协议定稿,省掉再调一轮。"""

    async def test_check_ok_auto_finishes_saving_a_round(self):
        """round 1 check_result 返回 "OK (N rows)" → 单轮结束,无第二次 chat。"""
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "check_result",
                 "arguments": '{"sql": "SELECT name FROM students"}'},
            ]},
        ])
        result = await run_agent_loop(
            llm, "m", "sys", "user", registry=_check_registry(), max_rounds=5,
        )
        assert result["finish_tool"] is True
        assert result["content"] == "SELECT name FROM students"
        assert result["rounds"] == 1
        assert result["guard_hit"] is False
        assert len(llm.calls) == 1  # 第二轮 chat_full 未发生

    async def test_auto_finish_not_on_violation(self):
        """VIOLATION → 不自动定稿,违例观测回喂,循环继续。"""
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "check_result",
                 "arguments": '{"sql": "SELECT bad FROM t"}'},
            ]},
            {"content": "done", "tool_calls": []},
        ])
        result = await run_agent_loop(
            llm, "m", "sys", "user", registry=_check_registry(), max_rounds=5,
        )
        assert result["finish_tool"] is False
        assert result["content"] == "done"
        assert result["rounds"] == 2
        assert "VIOLATION [F2]" in llm.calls[1][-1]["content"]

    async def test_explicit_finish_wins_over_auto_finish(self):
        """同轮显式 finish 与 check OK 并存 → 显式 finish 的载荷胜出。"""
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "check_result",
                 "arguments": '{"sql": "SELECT name FROM students"}'},
                {"id": "c2", "name": "finish", "arguments": '{"answer": "SELECT explicit"}'},
            ]},
        ])
        result = await run_agent_loop(
            llm, "m", "sys", "user", registry=_check_registry(), max_rounds=5,
        )
        assert result["finish_tool"] is True
        assert result["content"] == "SELECT explicit"
        assert result["rounds"] == 1

    async def test_auto_finish_picks_last_passing_check(self):
        """同轮多个 check OK → 取最后(最新)一个的 SQL 定稿。"""
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "check_result",
                 "arguments": '{"sql": "SELECT v1 FROM students"}'},
                {"id": "c2", "name": "check_result",
                 "arguments": '{"sql": "SELECT v2 FROM students"}'},
            ]},
        ])
        result = await run_agent_loop(
            llm, "m", "sys", "user", registry=_check_registry(), max_rounds=5,
        )
        assert result["content"] == "SELECT v2 FROM students"
        assert result["rounds"] == 1

    async def test_violation_after_ok_prevents_auto_finish(self):
        """最后(最新)一个 check 是 VIOLATION → 不自动定稿(模型仍在修正)。"""
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "check_result",
                 "arguments": '{"sql": "SELECT v1 FROM students"}'},
                {"id": "c2", "name": "check_result",
                 "arguments": '{"sql": "SELECT bad FROM t"}'},
            ]},
            {"content": "fixed", "tool_calls": []},
        ])
        result = await run_agent_loop(
            llm, "m", "sys", "user", registry=_check_registry(), max_rounds=5,
        )
        assert result["finish_tool"] is False
        assert result["content"] == "fixed"
        assert result["rounds"] == 2

    async def test_auto_finish_skips_empty_sql_payload(self):
        """check OK 但 sql 载荷为空 → 不自动定稿(无法交付,继续循环)。"""
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "check_result", "arguments": "{}"},
            ]},
            {"content": "done", "tool_calls": []},
        ])
        result = await run_agent_loop(
            llm, "m", "sys", "user", registry=_check_registry(), max_rounds=5,
        )
        assert result["finish_tool"] is False
        assert result["content"] == "done"
        assert result["rounds"] == 2


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

class TestErrorClassificationWiring:
    """错误分类在 harness 的落地:参数防火墙 + 按类重试决策 + [ERR:] 标注。"""

    async def test_args_schema_blocked_before_handler_run(self):
        """参数校验失败 → 不执行 handler,观测带 [ERR:ARGS_SCHEMA] 回喂模型。"""
        calls: list = []

        async def boom(arguments: dict) -> str:
            calls.append(1)
            return "ran"

        registry = ToolRegistry()
        registry.register(
            "need_sql", boom,
            description="needs sql",
            parameters={
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
        )
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [{"id": "c1", "name": "need_sql", "arguments": "{}"}]},
            {"content": "done", "tool_calls": []},
        ])
        result = await run_agent_loop(llm, "m", "sys", "user", registry=registry, max_rounds=5)
        assert calls == []                       # 防火墙拦截,未执行
        assert result["content"] == "done"
        obs = llm.calls[1][-1]["content"]
        assert "[ERR:ARGS_SCHEMA]" in obs
        assert "missing required field 'sql'" in obs

    async def test_permanent_tool_error_not_retried(self):
        """python 级错误(ValueError)→ 不消耗 retries,只折叠一次。"""
        calls = {"n": 0}

        async def boom(arguments: dict) -> str:
            calls["n"] += 1
            raise ValueError("kernel bug in handler")

        registry = ToolRegistry()
        registry.register("boom", boom, description="boom", retries=3, parameters={})
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [{"id": "c1", "name": "boom", "arguments": "{}"}]},
            {"content": "done", "tool_calls": []},
        ])
        result = await run_agent_loop(llm, "m", "sys", "user", registry=registry, max_rounds=5)
        assert calls["n"] == 1                   # 未盲目重试
        assert "[ERR:TOOL_RUNTIME]" in llm.calls[1][-1]["content"]

    async def test_transient_connection_error_retried(self):
        """瞬时连接类 → 按 spec.retries 退避重试(第 3 次成功)。"""
        calls = {"n": 0}

        async def flaky(arguments: dict) -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("Lost connection to MySQL server")
            return "recovered"

        registry = ToolRegistry()
        registry.register("flaky", flaky, description="x", retries=3, retry_base_delay=0.0, parameters={})
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [{"id": "c1", "name": "flaky", "arguments": "{}"}]},
            {"content": "done", "tool_calls": []},
        ])
        result = await run_agent_loop(llm, "m", "sys", "user", registry=registry, max_rounds=5)
        assert calls["n"] == 3
        assert "recovered" in result["tool_history"][0]["observation"]


class TestPromptCachingSplit:
    """cache_prefix 拆分:system/user 内容块断点 + 末工具定义断点。

    不传 cache_prefix 的调用方(planner/reflect 等)必须字节级原样——
    缓存断点开关绝不影响无稳定前缀的路径。
    """

    async def test_cache_prefix_splits_into_blocks(self):
        """system → 带断点的内容块列表;user → [稳定前缀+断点, 剩余]。"""
        llm = ScriptedLLM([{"content": "done", "tool_calls": []}])
        await run_agent_loop(
            llm, "anthropic/claude-opus-4",
            "sys rules", "stable-prefix\nquestion body",
            TOOL_DEF, {"echo": _echo}, max_rounds=2,
            cache_prefix="stable-prefix\n",
        )
        first = llm.calls[0]
        assert first[0] == {"role": "system", "content": [
            {"type": "text", "text": "sys rules",
             "cache_control": {"type": "ephemeral"}},
        ]}
        assert first[1] == {"role": "user", "content": [
            {"type": "text", "text": "stable-prefix\n",
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "question body"},
        ]}

    async def test_last_tool_def_gets_cache_control(self):
        """工具级缓存:仅最后一个工具定义打断点(Anthropic 工具缓存规则)。"""
        from types import SimpleNamespace

        captured: dict = {}

        async def fake_chat_full(model, messages, tools=None, **kwargs):
            captured["tools"] = tools
            captured["messages"] = list(messages)
            return {"content": "done", "tool_calls": []}

        tools = [
            {"type": "function", "function": {"name": "a", "description": "d", "parameters": {}}},
            {"type": "function", "function": {"name": "b", "description": "d", "parameters": {}}},
        ]
        await run_agent_loop(
            SimpleNamespace(chat_full=fake_chat_full),
            "anthropic/claude-opus-4", "sys", "prefix",
            tools, {"a": _echo, "b": _echo}, max_rounds=2,
            cache_prefix="pre",
        )
        assert "cache_control" not in captured["tools"][0]
        assert captured["tools"][1]["cache_control"] == {"type": "ephemeral"}

    async def test_no_split_without_cache_prefix(self):
        """不传 cache_prefix:字节级原样(planner/reflect 等调用方不受影响)。"""
        llm = ScriptedLLM([{"content": "done", "tool_calls": []}])
        await run_agent_loop(
            llm, "anthropic/claude-opus-4", "sys", "user",
            TOOL_DEF, {"echo": _echo}, max_rounds=2,
        )
        assert llm.calls[0] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "user"},
        ]
        # 工具定义也原样(无 cache_control)

    async def test_prompt_caching_off_keeps_plain_messages(self):
        """prompt_caching=False:传了 cache_prefix 也不拆。"""
        from types import SimpleNamespace

        captured: dict = {}

        async def fake_chat_full(model, messages, tools=None, **kwargs):
            captured["messages"] = list(messages)
            return {"content": "done", "tool_calls": []}

        await run_agent_loop(
            SimpleNamespace(chat_full=fake_chat_full),
            "anthropic/claude-opus-4", "sys", "user",
            TOOL_DEF, {"echo": _echo}, max_rounds=2,
            cache_prefix="stable-prefix\n", prompt_caching=False,
        )
        assert captured["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "user"},
        ]
