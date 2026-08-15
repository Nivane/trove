"""Run-trace replay command tests."""

import pytest

from trove.cli.slash_registry import SlashRegistry
from trove.cli.commands.trace_cmds import register_trace_commands
from trove.tracing.local import add_event, configure_trace_store


@pytest.fixture
def trace_home(tmp_home):
    configure_trace_store(str(tmp_home))
    return tmp_home


def make_reg():
    reg = SlashRegistry()
    register_trace_commands(reg, {})
    return reg


class TestTraceCommand:
    async def test_replay_latest_run(self, trace_home):
        add_event("r1", {"kind": "run", "question": "哪个地区平均贷款最高?", "session_id": "s"})
        add_event("r1", {"kind": "step", "seq": 1, "node": "schema_linking", "elapsed_ms": 5, "detail": {}})
        add_event("r1", {"kind": "llm", "node": "gen_sql", "model": "m",
                         "messages": [{"role": "user", "content": "生成 SQL"}],
                         "output": "SELECT 1", "elapsed_ms": 100})
        add_event("r1", {"kind": "finish", "summary": {"verdict": "OK", "retry_count": 0, "row_count": 3, "error": ""}})

        result = await make_reg().get("trace").handler("")
        assert "哪个地区平均贷款最高?" in result
        assert "schema_linking" in result
        assert "gen_sql" in result
        assert "SELECT 1" in result          # LLM 输出可见
        assert "生成 SQL" in result          # LLM 输入可见
        assert "verdict=OK" in result

    async def test_list_runs(self, trace_home):
        for rid, q in [("r1", "q1"), ("r2", "q2")]:
            add_event(rid, {"kind": "run", "question": q, "session_id": "s"})
            add_event(rid, {"kind": "finish", "summary": {"verdict": "OK"}})
        result = await make_reg().get("trace").handler("list")
        assert "q1" in result and "q2" in result

    async def test_empty_store(self, trace_home):
        result = await make_reg().get("trace").handler("")
        assert "暂无" in result
