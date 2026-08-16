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

    def test_corrupt_line_does_not_block_new_events(self, tmp_path):
        """历史文件里出现非 utf-8 坏行时,新事件仍能写入(追加而非整体重写)。"""
        _configure(tmp_path)
        (tmp_path / "traces.jsonl").write_bytes(b'\x93\xb6\xe8\xa1\x8c\n')  # 损坏行
        add_event("r1", {"kind": "run", "question": "q1"})
        add_event("r1", {"kind": "finish", "summary": {}})

        run = get_run("r1")
        assert run["question"] == "q1"
        assert [e["kind"] for e in run["events"]] == ["run", "finish"]

    def test_bad_lines_dropped_on_trim(self, tmp_path):
        """裁剪时坏行被丢弃,好行按最近 MAX_LINES 保留。"""
        from trove.tracing.local import MAX_LINES
        _configure(tmp_path)
        lines = [b'{"x": 1}\n' for _ in range(MAX_LINES)]
        (tmp_path / "traces.jsonl").write_bytes(b"".join(lines) + b'\x93\xb6\n')
        add_event("r1", {"kind": "run", "question": "after-trim"})

        run = get_run("r1")
        assert run["question"] == "after-trim"
        # 坏行被丢弃,不再阻塞后续写入
        add_event("r1", {"kind": "finish", "summary": {}})
        assert [e["kind"] for e in get_run("r1")["events"]] == ["run", "finish"]
