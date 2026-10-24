"""Agent-loop capture: raw tool observations vs the model-visible copy.

Every executed tool produces a ``tool`` event in ``results`` order (including
``finish`` and the auto-finalized ``check_result``). Only observations that
are actually fed back produce a ``tool_model`` event — a finished run's
``finish`` payload is the answer, not model input, so it has no
``tool_model``. That absence is evidence, not a gap.
"""

from __future__ import annotations

from trove.llm.agent_loop import ToolRegistry, run_agent_loop
from trove.tracing.run_record import (
    configure_run_records,
    drain_run_records,
    get_run_recorder,
    reset_run_records,
)


class ScriptedLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.calls.append(list(messages))
        return self._responses.pop(0)


class FakeStore:
    def __init__(self):
        self.writes: list[tuple[dict, list[dict]]] = []

    async def write(self, header, events):
        self.writes.append((dict(header), [dict(e) for e in events]))
        return len(events)


def _events(fake: FakeStore) -> list[dict]:
    assert fake.writes
    return fake.writes[-1][1]


async def _run(llm, registry, *, run_id="r1", rounds=5):
    result = await run_agent_loop(
        llm, "m", "sys", "user", registry=registry,
        max_rounds=rounds, metadata={"run_id": run_id},
    )
    rec = get_run_recorder()
    assert rec is not None
    rec.schedule_flush(run_id)
    await drain_run_records()
    return result


async def test_normal_tools_get_both_copies():
    fake = FakeStore()
    configure_run_records(fake, enabled=True)
    try:
        registry = ToolRegistry()

        async def echo(arguments):
            return f"echoed: {arguments['text']}"

        registry.register("echo", echo)
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "echo", "arguments": '{"text": "hi"}'},
            ]},
            {"content": "done", "tool_calls": []},
        ])
        result = await _run(llm, registry)
        assert result["content"] == "done"

        events = _events(fake)
        kinds = [e["kind"] for e in events]
        assert kinds == ["tool", "tool_model"]
        tool, model = events
        assert tool["payload"]["name"] == "echo"
        assert tool["payload"]["tool_call_id"] == "c1"
        assert tool["payload"]["observation"] == "echoed: hi"
        assert model["payload"]["tool_call_id"] == "c1"
        assert model["payload"]["content"] == "echoed: hi"
    finally:
        await drain_run_records()
        reset_run_records()


async def test_auto_check_result_captures_both_copies():
    """The auto-finalize path feeds the observation back → two events."""
    fake = FakeStore()
    configure_run_records(fake, enabled=True)
    try:
        registry = ToolRegistry()

        async def check_result(arguments):
            return "OK (3 rows)"

        registry.register("check_result", check_result)
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c9", "name": "check_result",
                 "arguments": '{"sql": "SELECT 1"}'},
            ]},
        ])
        result = await _run(llm, registry)
        assert result["content"] == "SELECT 1"
        assert result["rounds"] == 1

        kinds = [e["kind"] for e in _events(fake)]
        assert kinds == ["tool", "tool_model"]
    finally:
        await drain_run_records()
        reset_run_records()


async def test_finish_has_tool_event_but_no_tool_model_event():
    """finish 的载荷是答案,不是模型输入 —— 缺 tool_model 即证据。"""
    fake = FakeStore()
    configure_run_records(fake, enabled=True)
    try:
        registry = ToolRegistry(finish=True)
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "finish",
                 "arguments": '{"answer": "SELECT 1"}'},
            ]},
        ])
        result = await _run(llm, registry)
        assert result["content"] == "SELECT 1"

        events = _events(fake)
        assert [e["kind"] for e in events] == ["tool"]
        assert events[0]["payload"]["name"] == "finish"
        assert events[0]["payload"]["observation"] == "SELECT 1"
    finally:
        await drain_run_records()
        reset_run_records()


async def test_round_captures_all_tools_before_model_copies():
    """Raw events follow ``results`` order; copies follow the feedback loop."""
    fake = FakeStore()
    configure_run_records(fake, enabled=True)
    try:
        registry = ToolRegistry()

        async def tool_a(arguments):
            return "A"

        async def tool_b(arguments):
            return "B"

        registry.register("tool_a", tool_a)
        registry.register("tool_b", tool_b)
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "tool_a", "arguments": "{}"},
                {"id": "c2", "name": "tool_b", "arguments": "{}"},
            ]},
            {"content": "done", "tool_calls": []},
        ])
        await _run(llm, registry)

        events = _events(fake)
        kinds = [e["kind"] for e in events]
        assert kinds == ["tool", "tool", "tool_model", "tool_model"]
        assert [e["payload"].get("name") for e in events[:2]] == [
            "tool_a", "tool_b"]
        assert [e["payload"]["tool_call_id"] for e in events[2:]] == ["c1", "c2"]
    finally:
        await drain_run_records()
        reset_run_records()


async def test_no_run_id_captures_nothing():
    fake = FakeStore()
    configure_run_records(fake, enabled=True)
    try:
        registry = ToolRegistry()

        async def echo(arguments):
            return "x"

        registry.register("echo", echo)
        llm = ScriptedLLM([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "echo", "arguments": "{}"},
            ]},
            {"content": "done", "tool_calls": []},
        ])
        await run_agent_loop(
            llm, "m", "sys", "user", registry=registry, max_rounds=5)
        rec = get_run_recorder()
        assert rec is not None
        await rec.drain()
        assert rec._buffers == {}
        assert fake.writes == []
    finally:
        await drain_run_records()
        reset_run_records()
