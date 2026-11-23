"""--print JSON payload tests."""

import json

import pytest

from trove.main import format_print_payload, print_exit_code

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

    def test_refusal_included_when_present(self):
        """拒绝轮的 next_actions 要能被脚本直接消费,而不是只给一段 Markdown。"""
        refusal = {
            "reason": "no_model",
            "message": "该数据源还没有语义模型",
            "next_actions": [
                {"id": "init_kb", "kind": "navigate", "label": "去建语义模型",
                 "href": "/admin/kb?ds=demo", "admin_only": True},
            ],
        }
        payload = format_print_payload({**SUMMARY, "refusal": refusal}, [])
        assert payload["refusal"] == refusal
        # 非拒绝轮保持 None(缺席 ≠ 空对象),与 error_info 同一条纪律。
        assert format_print_payload(SUMMARY, [])["refusal"] is None
        json.dumps(payload)  # fully JSON-serializable


class TestPrintExitCode:
    """退出码只区分「运行崩了」；其余结局由 payload 的 outcome/refusal 表达。"""

    def test_failed_is_nonzero(self):
        assert print_exit_code({"outcome": "failed"}) == 1

    @pytest.mark.parametrize(
        "outcome",
        ["answered", "refused", "clarified", "pending_approval", "cancelled", None],
    )
    def test_terminal_outcomes_exit_zero(self, outcome):
        assert print_exit_code({"outcome": outcome}) == 0

    def test_absent_outcome_exits_zero(self):
        """旧 summary 没带 outcome：不猜,保持既有管道的退出行为。"""
        assert print_exit_code({}) == 0
        assert print_exit_code(format_print_payload(SUMMARY, [])) == 0
