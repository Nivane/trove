"""离线回放 eval 测试:打分纯函数 + 录制/回放 IO(零 LLM/网络/DB)。"""

from __future__ import annotations

import json

import pytest

from trove.eval.replay import (
    append_entry,
    format_entry,
    load_entries,
    normalize_sql,
    render_scorecard,
    score_replay,
    sql_exact_match,
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

    def test_correctness_requires_consensus(self):
        rows = [
            _entry(verdict="OK", consensus=True, n_candidates=5),
            _entry(verdict="OK", consensus=False, n_candidates=5),  # 平局 → 不计正确
            _entry(verdict="OK", consensus=True, n_candidates=0),   # 无候选 → 不计
        ]
        s = score_replay(rows)
        assert s["correctness"] == pytest.approx(1 / 3, abs=1e-3)

    def test_gold_match_optional(self):
        rows = [
            _entry(pred_sql="SELECT name FROM students", gold_sql="select name from students"),
            _entry(pred_sql="SELECT name FROM students", gold_sql="SELECT id FROM students"),
            _entry(pred_sql="SELECT 1"),  # 无 gold,不算分母
        ]
        s = score_replay(rows)
        assert s["gold_match"] == pytest.approx(0.5, abs=1e-3)
        assert s["gold_n"] == 2

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
