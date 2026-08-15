"""Local LLM call log tests — zero-config trace of prompt/response."""

import json

from trove.llm.call_log import record_call, read_recent


class TestCallLog:
    def test_record_and_read_roundtrip(self, tmp_home):
        record_call(
            str(tmp_home),
            metadata={"node": "gen_sql", "session_id": "s1"},
            model="deepseek/deepseek-chat",
            messages=[{"role": "user", "content": "问题"}],
            output="```sql\nSELECT 1;\n```",
            elapsed_ms=1234,
        )
        entries = read_recent(str(tmp_home), limit=10)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["model"] == "deepseek/deepseek-chat"
        assert entry["metadata"]["node"] == "gen_sql"
        assert entry["messages"][0]["content"] == "问题"
        assert "SELECT 1" in entry["output"]
        assert entry["elapsed_ms"] == 1234
        assert "ts" in entry

    def test_read_recent_limit_and_order(self, tmp_home):
        for i in range(5):
            record_call(
                str(tmp_home), metadata={}, model="m", messages=[],
                output=f"out{i}", elapsed_ms=1,
            )
        entries = read_recent(str(tmp_home), limit=3)
        assert [e["output"] for e in entries] == ["out2", "out3", "out4"]

    def test_missing_dir_yields_empty(self, tmp_home):
        assert read_recent(str(tmp_home / "nonexistent"), limit=5) == []
