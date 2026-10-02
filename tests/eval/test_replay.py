"""离线回放 eval 测试:打分纯函数 + 录制/回放 IO(零 LLM/网络/DB)。"""

from __future__ import annotations

import json

import pytest

from trove.eval.replay import (
    append_entry,
    completed,
    first_pass,
    format_entry,
    load_entries,
    normalize_sql,
    recovered,
    render_scorecard,
    score_replay,
    scorecard_metrics,
    sql_exact_match,
    tried_recovery,
    zero_answer,
)


def _entry(**kw):
    base = dict(
        run_id="r1", question="q", pred_sql="SELECT name FROM students",
        row_count=5, verdict="OK", retry_count=0, consensus=True,
        confidence=0.9, validation_hits=[], rollback_target="",
        fix_mode="", n_candidates=5, tokens={"prompt": 100, "completion": 20, "total": 120},
        elapsed_ms=1000, gold_sql="", kb_hits=[],
    )
    base.update(kw)
    return base


class TestNormalizeSql:
    def test_canonical_form(self):
        assert normalize_sql("select name from students") == "SELECT name FROM students"
        assert normalize_sql("  select   name  from students ; ") == "SELECT name FROM students"

    def test_empty(self):
        assert normalize_sql("") == ""
        assert normalize_sql(None) == ""

    def test_unparsable_falls_back(self):
        assert normalize_sql("SELEC * FROM students") != ""


class TestSqlExactMatch:
    def test_structural_equality(self):
        assert sql_exact_match(
            "SELECT name FROM students", "select name from students;")
        assert sql_exact_match("SELECT a FROM t", "select a from t")

    def test_difference(self):
        assert not sql_exact_match("SELECT name FROM students", "SELECT id FROM students")
        assert not sql_exact_match("", "SELECT 1")

    def test_empty_pred_not_match(self):
        assert not sql_exact_match("", "SELECT 1")


class TestScoreReplay:
    def test_empty_returns_zeros(self):
        s = score_replay([])
        assert s["n"] == 0
        assert s["completion_rate"] == 0.0
        assert s["gold_match"] is None

    def test_completion_and_cost(self):
        rows = [
            _entry(verdict="OK", pred_sql="SELECT 1", tokens={"total": 100}),
            _entry(verdict="OK", pred_sql="SELECT 2", tokens={"total": 200}),
            _entry(verdict="ERROR", pred_sql="", tokens={"total": 50}),
        ]
        s = score_replay(rows)
        assert s["n"] == 3
        assert s["completion_rate"] == pytest.approx(2 / 3, abs=1e-3)
        assert s["total_tokens"] == 350
        assert s["avg_tokens"] == pytest.approx(350 / 3, abs=0.05)

    def test_self_consistency_requires_consensus(self):
        rows = [
            _entry(verdict="OK", consensus=True, n_candidates=5),
            _entry(verdict="OK", consensus=False, n_candidates=5),  # 平局 → 不计自洽
            _entry(verdict="OK", consensus=True, n_candidates=0),   # 无候选 → 不计
        ]
        s = score_replay(rows)
        assert s["self_consistency"] == pytest.approx(1 / 3, abs=1e-3)
        # 旧名 correctness 是误称(它从来看的不是正确性),改名防复活
        assert "correctness" not in s

    def test_gold_match_optional(self):
        rows = [
            _entry(pred_sql="SELECT name FROM students", gold_sql="select name from students"),
            _entry(pred_sql="SELECT name FROM students", gold_sql="SELECT id FROM students"),
            _entry(pred_sql="SELECT 1"),  # 无 gold,不算分母
        ]
        s = score_replay(rows)
        assert s["gold_match"] == pytest.approx(0.5, abs=1e-3)
        assert s["gold_n"] == 2

    def test_ex_counts_only_match_over_judged(self):
        """EX 的分母是**可判题**(含各类错误),不是全部条目。"""
        rows = [
            _entry(verdict="MATCH"),
            _entry(verdict="MISMATCH"),
            _entry(verdict="EXECUTION_ERROR"),   # 报错即算错,进分母
            _entry(verdict="GENERATION_ERROR"),  # 同上
            _entry(verdict="EMPTY_SQL"),         # 同上
            _entry(verdict="OK"),                # replay 档判定,**不进**分母
        ]
        s = score_replay(rows)
        assert (s["ex_hit"], s["ex_judged"]) == (1, 5)
        assert s["ex"] == pytest.approx(0.2, abs=1e-3)

    def test_ex_moves_when_only_verdicts_change(self):
        """**反退化性质**:只改 verdict、别的字段一概不动,EX 必须跟着动。

        这是这个指标存在的全部理由。它挡的是一种具体的坏门:记分卡里
        每一项量的都是"过程"(跑完没有 / 候选一致没有 / 花了多少 token),
        于是把一条 MATCH 改成 MISMATCH,所有指标纹丝不动 —— 门对"答案对错"
        是瞎的,而它看起来是在跑。
        """
        before = [_entry(verdict="MATCH")] * 4
        after = [_entry(verdict="MATCH")] * 2 + [_entry(verdict="MISMATCH")] * 2
        a, b = score_replay(before), score_replay(after)

        assert a["ex"] == 1.0 and b["ex"] == pytest.approx(0.5, abs=1e-3)
        # 过程类指标**不该**动 —— 它们量的本来就不是对错
        assert a["completion_rate"] == b["completion_rate"]
        assert a["consensus_rate"] == b["consensus_rate"]
        assert a["total_tokens"] == b["total_tokens"]

    def test_ex_is_zero_on_empty(self):
        s = score_replay([])
        assert s["ex"] == 0.0 and s["ex_judged"] == 0

    def test_recovery_rate(self):
        rows = [
            _entry(retry_count=2, verdict="OK"),        # 尝试且成功
            _entry(retry_count=1, validation_hits=[{"n": "F1"}], verdict="OK"),  # 尝试且成功
            _entry(retry_count=3, verdict="ERROR"),     # 尝试但失败
            _entry(retry_count=0, verdict="OK"),        # 未触发恢复
        ]
        s = score_replay(rows)
        assert s["recovery_attempts"] == 3
        assert s["recovery_rate"] == pytest.approx(2 / 3, abs=1e-3)

    def test_quality_metrics(self):
        rows = [
            _entry(verdict="OK", consensus=True, confidence=0.9, n_candidates=5),
            _entry(verdict="OK", consensus=True, confidence=0.8, n_candidates=3),
            _entry(verdict="OK", consensus=False, confidence=0.4, n_candidates=5),
        ]
        s = score_replay(rows)
        assert s["consensus_rate"] == pytest.approx(2 / 3, abs=1e-3)
        assert s["avg_confidence"] == pytest.approx(0.7, abs=1e-3)
        assert s["avg_candidates"] == pytest.approx(13 / 3, abs=0.05)

    def test_zero_answer_and_first_pass(self):
        rows = [
            _entry(verdict="MATCH"),                  # 一次通过
            _entry(verdict="MATCH", retry_count=1),   # 恢复后才对 → 不算一次通过
            _entry(verdict="EMPTY_SQL", pred_sql=""),  # 零交付且没重试
        ]
        s = score_replay(rows)
        assert s["zero_answer"] == pytest.approx(1 / 3, abs=1e-3)
        assert s["first_pass"] == pytest.approx(1 / 3, abs=1e-3)

    def test_first_pass_omitted_without_judged_rows(self):
        # replay 档(OK 词表)没有可判题:first_pass 恒 0,发键 = 把"测不了"
        # 说成"测得零"。hasattr 式探测把"缺数据"与"数据是零"混为一谈。
        s = score_replay([_entry(verdict="OK"), _entry(verdict="OK")])
        assert "first_pass" not in s

    def test_elapsed_emitted_only_when_measured(self):
        s = score_replay([_entry()])  # _entry 默认 elapsed_ms=1000
        assert s["total_elapsed_ms"] == 1000.0
        assert s["avg_elapsed_ms"] == 1000.0
        s2 = score_replay([_entry(elapsed_ms=0)])
        assert "total_elapsed_ms" not in s2
        assert "avg_elapsed_ms" not in s2

    def test_ex_by_path_only_when_path_coverage_complete(self):
        rows = [
            _entry(verdict="MATCH", path="compiled"),
            _entry(verdict="MISMATCH", path="llm"),
        ]
        s = score_replay(rows)
        assert s["ex_by_path"] == {"compiled": 1.0, "llm": 0.0}
        # 半份 path:分档是把"缺数据"当"llm 档",不发键
        s2 = score_replay([_entry(verdict="MATCH", path="compiled"), _entry(verdict="MATCH")])
        assert "ex_by_path" not in s2


class TestUnifiedPredicates:
    """两引擎共用的唯一口径(replay.score_replay 与 gate.metrics_from_entries)。

    2026-10 实测过的漂移:同一份冻结文件,completion 两边算出 0.9375 与
    0.9062、recovery 算出 1.0 与 0.1765。谓词测试是防再漂移的第一道闸。
    """

    def test_tried_reads_both_retry_key_spellings(self):
        # replay.jsonl 写 retry_count,results.jsonl 写 retries —— 只读一个键,
        # 另一半题就从恢复率分母里漏掉(冻结基线实测漏 2 题)
        assert tried_recovery({"retry_count": 1}) is True
        assert tried_recovery({"retries": 1}) is True
        assert tried_recovery({"retry_count": 0, "retries": 0}) is False
        assert tried_recovery({"retry_count": 0, "validation_hits": [{"rule": "F1"}]}) is True

    def test_completed_excludes_hard_fail_verdicts(self):
        assert completed({"pred_sql": "SELECT 1", "verdict": "OK"}) is True
        # 答错也是交付过答案:完成 ≠ 正确,那是 ex 的事
        assert completed({"pred_sql": "SELECT 1", "verdict": "MISMATCH"}) is True
        assert completed({"pred_sql": "SELECT 1", "verdict": "EXECUTION_ERROR"}) is False
        assert completed({"pred_sql": "SELECT 1", "verdict": "EMPTY_SQL"}) is False
        assert completed({"pred_sql": "", "verdict": "OK"}) is False

    def test_recovered_is_strict(self):
        # 触发过恢复 ≠ 恢复成功:实测基线 17 次触发只有 3 次以 MATCH 收场
        assert recovered({"pred_sql": "SELECT 1", "verdict": "OK", "retry_count": 1}) is True
        assert recovered({"pred_sql": "SELECT 1", "verdict": "EXECUTION_ERROR",
                          "retry_count": 1}) is False
        assert recovered({"pred_sql": "SELECT 1", "verdict": "MISMATCH",
                          "retry_count": 1}) is False
        assert recovered({"pred_sql": "SELECT 1", "verdict": "OK", "retry_count": 0}) is False

    def test_zero_answer_invisible_to_recovery(self):
        # 0483/0487 型:没交付且没重试 —— 旧口径在 recovery 的分子分母里
        # 都看不见它
        e = {"pred_sql": "", "verdict": "EMPTY_SQL", "retry_count": 0}
        assert zero_answer(e) is True
        assert tried_recovery(e) is False

    def test_first_pass_requires_match_without_recovery(self):
        assert first_pass({"verdict": "MATCH", "retry_count": 0}) is True
        assert first_pass({"verdict": "MATCH", "retry_count": 1}) is False
        assert first_pass({"verdict": "MISMATCH", "retry_count": 0}) is False


class TestRecordIo:
    def test_append_and_load_roundtrip(self, tmp_path):
        p = tmp_path / "replay.jsonl"
        append_entry(p, format_entry("r1", "q1", pred_sql="SELECT 1", verdict="OK"))
        append_entry(p, format_entry("r2", "q2", pred_sql="SELECT 2", verdict="OK"))
        rows = load_entries(p)
        assert len(rows) == 2
        assert rows[0]["run_id"] == "r1"
        assert rows[1]["question"] == "q2"

    def test_load_skips_bad_lines(self, tmp_path):
        p = tmp_path / "replay.jsonl"
        p.write_text('{"ok": true}\nnot-json\n{"n": 1}\n', encoding="utf-8")
        assert len(load_entries(p)) == 2

    def test_load_missing_file(self, tmp_path):
        assert load_entries(tmp_path / "nope.jsonl") == []

    def test_format_entry_defaults(self):
        e = format_entry("r1", "q1")
        assert e["pred_sql"] == ""
        assert e["tokens"] == {}
        assert e["validation_hits"] == []
        assert json.dumps(e, ensure_ascii=False)  # 可序列化


class TestRenderScorecard:
    def test_scorecard_contains_key_metrics(self):
        s = score_replay([_entry(verdict="OK")] * 2)
        text = render_scorecard(s)
        assert "完成率" in text and "token" in text and "失败恢复率" in text
        assert "自洽率" in text and "correctness" not in text


class TestScorecardMetrics:
    """score_replay → 门禁/基线指标快照的唯一映射(CLI / CI 重钉 / pin 测试共用)。"""

    def test_key_set_matches_gate_vocabulary(self):
        s = score_replay([_entry(verdict="MATCH", gold_sql="SELECT name FROM students")])
        m = scorecard_metrics(s)
        assert {
            "ex", "completion", "self_consistency", "recovery", "zero_answer",
            "consensus_rate", "avg_confidence", "avg_tokens", "total_tokens",
            "n", "n_judged", "first_pass", "gold_match",
        } <= set(m)
        assert "correctness" not in m

    def test_conditional_keys_follow_score(self):
        s = score_replay([_entry(verdict="OK", elapsed_ms=0, gold_sql="")])
        m = scorecard_metrics(s)
        assert "first_pass" not in m    # OK 词表无判题
        assert "gold_match" not in m    # 无 gold
        assert "avg_elapsed_ms" not in m  # 未测墙钟
