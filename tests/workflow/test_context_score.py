"""Context relevance scoring + per-turn history parsing tests."""

from trove.workflow.context_score import (
    history_items,
    parse_history_turns,
    relevance_score,
    text_ngrams,
)


class TestTextNgrams:
    def test_english_words_and_cjk_bigrams(self):
        feats = text_ngrams("北京 平均 grade avg")
        assert "grade" in feats
        assert "avg" in feats
        assert "北京" in feats  # 中文按字 bigram


class TestRelevanceScore:
    def test_shared_terms_positive(self):
        assert relevance_score("what is the avg grade in 北京", "北京 平均 grade") > 0

    def test_no_overlap_zero(self):
        assert relevance_score("totally unrelated", "北京 平均 grade") == 0.0

    def test_empty_question_zero(self):
        assert relevance_score("anything", "") == 0.0

    def test_score_bounded(self):
        s = relevance_score("grade grade grade grade", "grade")
        assert 0.0 <= s <= 1.0


class TestParseHistoryTurns:
    def test_flat_history_splits_by_role(self):
        history = "user: 平均成绩是多少\nassistant: 85 分"
        turns = parse_history_turns(history)
        assert turns == [
            {"role": "user", "text": "平均成绩是多少"},
            {"role": "assistant", "text": "85 分"},
        ]

    def test_summary_kept_as_own_turn(self):
        history = "[summary] 之前讨论过成绩\nuser: 现在呢"
        turns = parse_history_turns(history)
        assert turns[0]["role"] == "summary"
        assert turns[0]["text"] == "之前讨论过成绩"

    def test_continuation_lines_merge_into_previous(self):
        history = "assistant: 85 分\n这是均值"
        turns = parse_history_turns(history)
        assert turns[-1]["text"] == "85 分 这是均值"


class TestHistoryItems:
    def test_scored_and_keyed_per_turn(self):
        history = "user: 北京平均成绩\nassistant: 85 分\nuser: 无关话题"
        items = history_items(history, question="北京 平均成绩")
        assert [it.key for it in items] == ["turn0", "turn1", "turn2"]
        # 与问句相关的轮(北京平均成绩)分数高于无关轮
        assert items[0].score > items[2].score

    def test_recent_turn_gets_recency_bonus(self):
        history = "user: 无关话题\nuser: 北京平均成绩"
        items = history_items(history, question="北京 平均成绩")
        # 相关 + 更近 → 分数最高
        assert items[1].score > items[0].score

    def test_empty_history(self):
        assert history_items("", question="q") == []
