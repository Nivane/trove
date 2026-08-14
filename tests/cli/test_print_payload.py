"""--print JSON payload tests."""

import json

from trove.main import format_print_payload

SUMMARY = {
    "session_id": "s1",
    "question": "q",
    "sql": "SELECT 1",
    "row_count": 2,
    "verdict": "OK",
    "reason": "",
    "error": "",
    "final_response": "## Answer",
}


class TestFormatPrintPayload:
    def test_payload_shape(self):
        events = [
            {"type": "thought", "node": "start", "content": "Processing..."},
            {"type": "sql", "node": "gen_sql", "content": "SELECT 1"},
            {"type": "done", "content": "## Answer", "summary": SUMMARY},
        ]
        payload = format_print_payload(SUMMARY, events)
        assert payload["session_id"] == "s1"
        assert payload["response"] == "## Answer"
        assert payload["sql"] == "SELECT 1"
        assert payload["row_count"] == 2
        assert payload["verdict"] == "OK"
        assert payload["error"] == ""

    def test_events_are_serializable_and_drop_summary(self):
        events = [{"type": "done", "content": "x", "summary": SUMMARY}]
        payload = format_print_payload(SUMMARY, events)
        assert payload["events"] == [{"type": "done", "content": "x"}]
        json.dumps(payload)  # fully JSON-serializable

    def test_error_field_included(self):
        summary = {**SUMMARY, "error": "boom"}
        payload = format_print_payload(summary, [])
        assert payload["error"] == "boom"
