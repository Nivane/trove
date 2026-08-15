"""Local run-trace store tests — full trajectory recording."""

from trove.tracing.local import (
    add_event,
    configure_trace_store,
    list_recent_runs,
    get_run,
)


def _configure(tmp_path):
    home = str(tmp_path)
    configure_trace_store(home)
    return home


class TestTraceStore:
    def test_full_run_roundtrip(self, tmp_path):
        _configure(tmp_path)
        add_event("r1", {"kind": "run", "session_id": "s1", "question": "q1", "ts": 100})
        add_event("r1", {"kind": "step", "seq": 1, "node": "schema_linking", "elapsed_ms": 5})
        add_event("r1", {"kind": "llm", "node": "gen_sql", "model": "m",
                         "messages": [{"role": "user", "content": "问题"}],
                         "output": "SELECT 1", "elapsed_ms": 800})
        add_event("r1", {"kind": "finish", "summary": {"verdict": "OK", "retry_count": 0}})

        run = get_run("r1")
        assert run["question"] == "q1"
        kinds = [e["kind"] for e in run["events"]]
        assert kinds == ["run", "step", "llm", "finish"]
        llm_event = run["events"][2]
        assert llm_event["messages"][0]["content"] == "问题"
        assert llm_event["output"] == "SELECT 1"

    def test_list_recent_runs(self, tmp_path):
        _configure(tmp_path)
        for rid, q in [("r1", "q1"), ("r2", "q2"), ("r3", "q3")]:
            add_event(rid, {"kind": "run", "question": q, "ts": 1})
            add_event(rid, {"kind": "finish", "summary": {}})
        recent = list_recent_runs(limit=2)
        assert [r["question"] for r in recent] == ["q2", "q3"]

    def test_missing_run_returns_empty(self, tmp_path):
        _configure(tmp_path)
        assert get_run("nope") == {"events": []}
