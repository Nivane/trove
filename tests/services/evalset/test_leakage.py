"""泄漏判据:归一化用 ledger 尺子(不折 _),与评测对账键刻意不同。"""
from __future__ import annotations

from trove.eval.text import norm_question
from trove.services.evalset.leakage import (
    bigram_jaccard,
    check_leakage,
    load_example_questions,
    word_overlap,
)


def test_ledger_ruler_keeps_underscore_but_eval_ruler_folds_it():
    from trove.services.kb.ledger import normalize_question as ledger_norm
    assert ledger_norm("Loan_Amt") == "loan_amt"
    assert norm_question("Loan_Amt") == "loan amt"
    # ledger 归一相同 → 泄漏;评测键相同不代表 ledger 相同
    hit = check_leakage("Loan_Amt by region", ["loan_amt by region"])
    assert hit is not None and hit.kind == "normalized"


def test_word_overlap_matches_graphs_ruler():
    from trove.workflow.graphs import _word_overlap
    samples = [
        ("total loan amount by district", "loan amount by district"),
        ("loan amount", "total loan amount by district"),
        ("", "anything"), ("a_b c", "a b c"),
    ]
    for a, b in samples:
        assert word_overlap(a, b) == _word_overlap(a, b)


def test_english_near_duplicate_hits_word_rule():
    hit = check_leakage("what is the total loan amount by district",
                        ["what is total loan amount by district"])
    assert hit is not None and hit.kind == "word"


def test_chinese_near_duplicate_hits_bigram_rule():
    # R2:一字之差不够 0.80(8/10 = 0.70);一字追加 = 8/9 ≈ 0.889。
    hit = check_leakage("按地区统计贷款总额", ["按地区统计贷款总额吗"])
    assert hit is not None and hit.kind == "bigram"
    assert bigram_jaccard("按地区统计贷款总额", "按地区统计贷款总额吗") >= 0.8


def test_punctuation_and_spacing_only_differences_are_near_duplicates():
    # R8:ledger 归一在标点处置空格,bigram 成对前剥掉空白 —— 只有标点/空格
    # 不同的问句仍是近重复(分数 1.0,不是 0.8x)。
    hit = check_leakage("按地区统计，贷款总额是多少？",
                        ["按地区统计贷款总额是多少？"])
    assert hit is not None and hit.kind == "bigram"
    assert hit.score == 1.0


def test_bigram_jaccard_short_inputs_are_zero():
    assert bigram_jaccard("", "abc") == 0.0
    assert bigram_jaccard("a", "a") == 0.0


def test_normalized_pass_outranks_earlier_word_hit():
    # 判据优先级高于示例文件顺序:晚出现的 normalized 命中赢过早出现的 word 命中。
    question = "what is the total loan amount by district"
    hit = check_leakage(question, [
        "what is total loan amount by district",
        "What is the total loan amount by district?",
    ])
    assert hit is not None and hit.kind == "normalized"
    assert hit.question == "What is the total loan amount by district?"


def test_unrelated_is_not_leakage():
    assert check_leakage("按地区统计贷款总额", ["每个月的开户数量"]) is None


def test_load_example_questions_reads_confirmed_and_pending(tmp_path):
    path = tmp_path / "examples.yml"
    path.write_text(
        "examples:\n"
        "  - question: 已确认题\n    sql: SELECT 1\n    pending: false\n"
        "  - question: 待确认题\n    sql: SELECT 2\n    pending: true\n"
        "  - sql: SELECT 3\n", encoding="utf-8")
    assert load_example_questions(path) == ["已确认题", "待确认题"]
    assert load_example_questions(tmp_path / "missing.yml") == []


def test_load_example_questions_tolerates_bad_yaml(tmp_path):
    not_mapping = tmp_path / "list.yml"
    not_mapping.write_text("- just\n- a list\n", encoding="utf-8")
    assert load_example_questions(not_mapping) == []
    broken = tmp_path / "broken.yml"
    broken.write_text("examples: [unclosed\n", encoding="utf-8")
    assert load_example_questions(broken) == []


def test_hit_note_names_the_rule_and_example():
    hit = check_leakage("loan_amt by region", ["loan_amt by region"])
    assert hit.note() == "normalized: loan_amt by region"
