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

    def test_error_info_included_when_present(self):
        """ERR-1/ONB-4:机器消费方拿到结构化错误,而不是只有一句标题。"""
        info = {
            "kind": "model",
            "title": "模型不可用",
            "retryable": False,
            "detail": {"raw": "invalid api key", "node": "workflow"},
        }
        payload = format_print_payload({**SUMMARY, "error_info": info}, [])
        assert payload["error_info"] == info
        assert format_print_payload(SUMMARY, [])["error_info"] is None

    def test_kb_hits_included_when_present(self):
        hits = [{"kind": "term", "term": "平均成绩", "mapping": "AVG(grade)"}]
        payload = format_print_payload({**SUMMARY, "kb_hits": hits}, [])
        assert payload["kb_hits"] == hits
        # absent → empty list, still serializable
        assert format_print_payload(SUMMARY, [])["kb_hits"] == []

    def test_outcome_included(self):
        """--print 也是一条对外出口:拒绝/澄清必须与答复分得开。"""
        payload = format_print_payload({**SUMMARY, "outcome": "refused"}, [])
        assert payload["outcome"] == "refused"
        # 缺席 = 更老的 summary,不按 answered 兜底。
        assert format_print_payload(SUMMARY, [])["outcome"] is None
