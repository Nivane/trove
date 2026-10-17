"""agent-loop 出境脱敏接线:唯一收口点覆盖普通循环与自动定稿两条路径。

脱敏引擎本身的行为在 ``tests/services/authz/test_egress.py``;这里只钉接线:

- 普通循环回喂的观测是**脱敏后**的;
- 自动定稿(check_result → OK)也走同一收口点,不需要第二处接线;
- redactor 抛异常 → 观测整体扣留(fail-closed),原文绝不回喂;
- 未装配 redactor → 观测逐字节不变;
- ``tool_history``(审计面)保留原文;**finish 载荷(SQL)是控制值,不经脱敏**。
"""

from __future__ import annotations

from trove.llm.agent_loop import ToolRegistry, run_agent_loop

RAW = "13812348888"
MASKED = "138****8888"
WITHHELD = "[ERR:EGRESS_REDACT] observation withheld"


class ScriptedLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.calls.append(list(messages))
        return self._responses.pop(0)


class FakeRedactor:
    """鸭子类型 redactor:记录调用,可指定替换值或抛异常。"""

    def __init__(self, replace=MASKED, error: Exception | None = None):
        self.replace = replace
        self.error = error
        self.calls: list[tuple[str, dict, object]] = []

    def redact_tool_result(self, name, args, value):
        self.calls.append((name, dict(args), value))
        if self.error is not None:
            raise self.error
        if self.replace is None:
            return value, {}
        report = {"tool": name, "fields": {"phone": "partial"}, "rows": 1, "values": 0}
        return self.replace, report


def _tc(call_id: str, name: str, arguments: str = "{}") -> dict:
    return {"id": call_id, "name": name, "arguments": arguments}


def _registry(redactor=None, **kwargs) -> ToolRegistry:
    registry = ToolRegistry(egress_redactor=redactor, **kwargs)

    async def echo(arguments):
        return {"phone": RAW, "city": "sh"}

    async def check(arguments):
        return {
            "ok": True, "row_count": 1,
            "columns": ["phone"], "rows": [[RAW]],
        }

    registry.register("echo", echo)
    registry.register("check_result", check)
    return registry


class TestNormalLoop:
    async def test_observation_redacted_before_model(self):
        redactor = FakeRedactor()
        registry = _registry(redactor)
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [_tc("c1", "echo")]},
            {"content": "done", "tool_calls": []},
        ])
        result = await run_agent_loop(
            llm, "m", "sys", "user", registry=registry, max_rounds=5,
        )
        tool_msg = llm.calls[1][-1]
        assert tool_msg["role"] == "tool"
        assert MASKED in tool_msg["content"]
        assert RAW not in tool_msg["content"]
        # 审计面保留原文
        assert RAW in result["tool_history"][0]["observation"]
        # 报告进注册表(节点汇总后写 state / 审计)
        assert registry.egress_report["echo"]["rows"] == 1
        assert registry.egress_report["echo"]["fields"] == {"phone": "partial"}

    async def test_no_redactor_is_byte_identical(self):
        registry = _registry()
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [_tc("c1", "echo")]},
            {"content": "done", "tool_calls": []},
        ])
        await run_agent_loop(llm, "m", "sys", "user", registry=registry, max_rounds=5)
        assert '{"phone": "13812348888", "city": "sh"}' in llm.calls[1][-1]["content"]
        assert registry.egress_report == {}

    async def test_noop_redactor_is_byte_identical(self):
        """redactor 返回原值 + 空报告 → 观测与无 redactor 完全相同。"""
        registry = _registry(FakeRedactor(replace=None))
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [_tc("c1", "echo")]},
            {"content": "done", "tool_calls": []},
        ])
        await run_agent_loop(llm, "m", "sys", "user", registry=registry, max_rounds=5)
        assert '{"phone": "13812348888", "city": "sh"}' in llm.calls[1][-1]["content"]
        assert registry.egress_report == {}

    async def test_redactor_failure_withholds_observation(self):
        registry = _registry(FakeRedactor(error=RuntimeError("salt missing")))
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [_tc("c1", "echo")]},
            {"content": "done", "tool_calls": []},
        ])
        result = await run_agent_loop(
            llm, "m", "sys", "user", registry=registry, max_rounds=5,
        )
        content = llm.calls[1][-1]["content"]
        assert WITHHELD in content
        assert RAW not in content
        assert registry.egress_report["echo"]["withheld"] == 1
        # 审计面仍是原文(扣留只作用于模型接收方)
        assert RAW in result["tool_history"][0]["observation"]


class TestAutoFinalize:
    async def test_check_result_finalize_uses_same_chokepoint(self):
        """自动定稿的观测也经过 redactor(同一条 ``_model_observation``)。"""
        redactor = FakeRedactor()
        registry = _registry(redactor)
        # check_result 的契约文本以 "OK (" 开头 → harness 自动定稿
        async def ok_check(arguments):
            return f"OK (1 rows) phone={RAW}"

        registry.register("check_result", ok_check)
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                _tc("c1", "check_result", '{"sql": "SELECT phone FROM customers"}'),
            ]},
        ])
        result = await run_agent_loop(
            llm, "m", "sys", "user", registry=registry, max_rounds=5,
        )
        assert result["finish_tool"] is True
        assert result["content"] == "SELECT phone FROM customers"
        assert any(name == "check_result" for name, _, _ in redactor.calls)
        # 定稿判定读的是原始观测(startswith "OK ("),不是脱敏后的
        assert result["tool_history"][0]["observation"].startswith("OK (")

    async def test_violation_observation_redacted_and_loop_continues(self):
        """check_result 返回 VIOLATION → 观测回喂下一轮,且已脱敏。"""
        redactor = FakeRedactor()
        registry = _registry(redactor)
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                _tc("c1", "check_result", '{"sql": "SELECT phone FROM customers"}'),
            ]},
            {"content": "retrying", "tool_calls": []},
        ])
        await run_agent_loop(
            llm, "m", "sys", "user", registry=registry, max_rounds=5,
        )
        assert MASKED in llm.calls[1][-1]["content"]
        assert RAW not in llm.calls[1][-1]["content"]


class TestFinishPayload:
    async def test_finish_payload_bypasses_redaction(self):
        """finish 载荷是控制值(SQL),不是数据观测:原样定稿。"""
        redactor = FakeRedactor()
        registry = _registry(redactor, finish=True)
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                _tc("c1", "finish", '{"answer": "SELECT phone FROM customers"}'),
            ]},
        ])
        result = await run_agent_loop(
            llm, "m", "sys", "user", registry=registry, max_rounds=5,
        )
        assert result["finish_tool"] is True
        assert result["content"] == "SELECT phone FROM customers"
        assert redactor.calls == []
        assert registry.egress_report == {}

    async def test_unknown_tool_error_path_also_redacted(self):
        """错误观测(无 ``value``,退回 ``observation`` 文本)同样过收口。"""
        redactor = FakeRedactor()
        registry = _registry(redactor)
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [_tc("c1", "ghost")]},
            {"content": "done", "tool_calls": []},
        ])
        await run_agent_loop(llm, "m", "sys", "user", registry=registry, max_rounds=5)
        # redactor 被调用(未知工具也走同一收口),替换值回喂
        assert redactor.calls[0][0] == "ghost"
        assert MASKED in llm.calls[1][-1]["content"]
