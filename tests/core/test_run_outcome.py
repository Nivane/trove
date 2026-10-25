"""运行结局词汇（P2）：六值闭集、判定优先级、降级不是结局。"""
from __future__ import annotations

import pytest

from trove.core.run_outcome import (
    RECORD_STATUS_OUTCOME,
    RUN_OUTCOMES,
    classify_run,
    record_outcome,
)


class TestVocabulary:
    def test_exactly_six_closed_values(self):
        assert RUN_OUTCOMES == (
            "answered",
            "refused",
            "clarified",
            "pending_approval",
            "cancelled",
            "failed",
        )
        assert len(set(RUN_OUTCOMES)) == len(RUN_OUTCOMES)

    def test_degradation_is_a_modifier_not_an_outcome(self):
        # 「降级」回答的是「哪一步没做成」,不是「这次运行怎么了」。
        # 一个带降级标记的答复仍然是一次答复,标记留在自己的载荷里可见。
        assert "degraded" not in RUN_OUTCOMES
        assert classify_run() == "answered"


class TestClassifyRun:
    @pytest.mark.parametrize(
        ("signals", "expected"),
        [
            ({}, "answered"),
            ({"clarification_question": "你说的地区是指哪个?"}, "clarified"),
            ({"refusal": {"reason": "no_model"}}, "refused"),
            ({"error": "connector down"}, "failed"),
            ({"hitl_status": "approved"}, "answered"),
            ({"hitl_status": "pending"}, "pending_approval"),
            ({"hitl_status": "rejected"}, "cancelled"),
        ],
    )
    def test_each_ending(self, signals, expected):
        assert classify_run(**signals) == expected

    def test_intent_answers_count_as_answered(self):
        # 写作/闲聊/纠正/草稿确认引导都是「给了回复」,不是第四种结局——
        # 它们只体现在 final_response 的内容里,所以这里没有任何信号参数。
        assert classify_run() == "answered"

    def test_refusal_wins_over_clarification(self):
        # 拒绝轮同时写 clarification_question;判成澄清会让拒绝从记录里消失。
        assert (
            classify_run(
                refusal={"reason": "no_model"},
                clarification_question="换个问法?",
            )
            == "refused"
        )

    def test_string_refusal_is_still_a_refusal(self):
        # MCP 的禁用数据源拒绝对是一个纯字符串,不是 dict。
        assert classify_run(refusal="datasource not allowed") == "refused"

    def test_error_wins_over_refusal_and_clarification(self):
        # 响亮优先:没跑完的运行不能冒充一次正常的拒绝/澄清。
        assert (
            classify_run(
                error="boom",
                refusal={"reason": "x"},
                clarification_question="q",
            )
            == "failed"
        )

    def test_pending_approval_wins_over_everything(self):
        # 图在人工确认前就停了,那一刻没有答案可言。
        assert (
            classify_run(
                hitl_status="pending",
                error="boom",
                refusal={"reason": "x"},
            )
            == "pending_approval"
        )

    def test_unknown_hitl_status_falls_through(self):
        # 分类器绝不能成为一次运行失败的原因。
        assert classify_run(hitl_status="weird") == "answered"


class TestRecordOutcome:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("running", None),
            ("pending_hitl", "pending_approval"),
            ("done", None),
            ("failed", "failed"),
            ("error", "failed"),
            ("cancelled", "cancelled"),
        ],
    )
    def test_lifecycle_mapping(self, status, expected):
        assert status in RECORD_STATUS_OUTCOME
        assert record_outcome(status) == expected

    def test_finish_outcome_speaks_for_done(self):
        # done 是生命周期终点:答复/拒绝/澄清都落 done,差别全在 finish 里。
        for outcome in RUN_OUTCOMES:
            assert record_outcome("done", {"outcome": outcome}) == outcome

    def test_finish_outcome_wins_over_the_mapping(self):
        assert record_outcome("failed", {"outcome": "refused"}) == "refused"

    def test_malformed_finish_falls_back_to_the_mapping(self):
        # 读端不信 finish 里的野值,但也不因此丢掉状态自带的结局。
        assert record_outcome("failed", {"outcome": "weird"}) == "failed"
        assert record_outcome("done", {"outcome": "weird"}) is None

    def test_unknown_status_raises(self):
        # 闭集不许静默兜底:新状态必须先在这里表态。
        with pytest.raises(ValueError):
            record_outcome("bogus")

    def test_unknown_status_raises_even_with_a_valid_finish(self):
        with pytest.raises(ValueError):
            record_outcome("bogus", {"outcome": "answered"})
