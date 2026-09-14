"""离线评测回归门测试:指标计算 + 方向/容差判定 + 渲染(纯函数,零 IO)。"""

from __future__ import annotations

import json

import pytest

from trove.eval.gate import (
    compare_metrics,
    load_entries,
    metrics_from_entries,
    render_report,
    score_from_file,
)


def _eval_entry(verdict="MATCH", compile_outcome=None, retries=0, **kw):
    e = {
        "run_id": "e1", "question": "q", "gold_sql": "", "verdict": verdict,
        "pred_sql": "SELECT 1", "retries": retries,
        "consensus": True, "confidence": 0.9,
        "validation_hits": [], "rollback_target": "", "fix_mode": "",
    }
    if compile_outcome is not None:
        e["compile_meta"] = {"outcome": compile_outcome}
    e.update(kw)
    return e


class TestMetricsFromEntries:
    def test_ex_and_compile_hit(self):
        rows = [
            _eval_entry("MATCH", compile_outcome="compiled"),
            _eval_entry("MISMATCH", compile_outcome="compiled"),
            _eval_entry("GENERATION_ERROR", compile_outcome="miss"),
            _eval_entry("EXECUTION_ERROR"),  # 无 compile_meta
            _eval_entry("GOLD_ERROR"),       # gold 失败,不进可判定
        ]
        m = metrics_from_entries(rows)
        # 可判定 4 题(excl GOLD_ERROR),MATCH 1 → 0.25
        assert m["ex"] == 0.25
        # 有 compile_meta 的 3 题,compiled 2 → 0.6667
        assert round(m["compile_hit"], 3) == 0.667
        assert m["n"] == 5

    def test_eval_bird_entries_with_tokens(self):
        """eval_bird 判定条目带 tokens(进程级记账注入)→ token 指标生效。"""
        rows = [
            _eval_entry("MATCH", tokens={"prompt": 800, "completion": 100, "total": 900}),
            _eval_entry("MISMATCH", tokens={"prompt": 600, "completion": 50, "total": 650}),
        ]
        m = metrics_from_entries(rows)
        assert m["total_tokens"] == 1550
        assert m["avg_tokens"] == 775

    def test_replay_entries_completion_and_gold(self):
        rows = [
            {"run_id": "r1", "question": "q1", "pred_sql": "SELECT 1",
             "verdict": "OK", "retry_count": 0, "consensus": True,
             "tokens": {"total": 100}, "gold_sql": "select 1",
             "validation_hits": [], "rollback_target": "", "fix_mode": ""},
            {"run_id": "r2", "question": "q2", "pred_sql": "SELECT 2",
             "verdict": "ERROR", "error": "generation", "retry_count": 1,
             "gold_sql": "SELECT 3", "tokens": {"total": 50},
             "validation_hits": [], "rollback_target": "", "fix_mode": ""},
        ]
        m = metrics_from_entries(rows)
        # 无 MATCH 体系 → ex=0(非 replay 维度);完成率 1/2
        assert m["completion"] == 0.5
        # gold 精确匹配:1/2
        assert m["gold_match"] == 0.5
        # token:total=150, avg=75
        assert m["total_tokens"] == 150
        assert m["avg_tokens"] == 75
        # 恢复率:1 次触发(r2),成功 0 → 0
        assert m["recovery"] == 0.0

    def test_empty_entries(self):
        m = metrics_from_entries([])
        assert m["ex"] == 0.0
        assert m["n"] == 0.0
        assert m["n_judged"] == 0.0

    def test_recovery_rate(self):
        rows = [
            _eval_entry("MATCH", retries=2),                      # 尝试且成功
            _eval_entry("MATCH", validation_hits=[{"n": "F1"}]),  # 尝试且成功
            _eval_entry("MISMATCH", retries=3),                   # 尝试且失败
            _eval_entry("MATCH", retries=0),                      # 未触发
        ]
        m = metrics_from_entries(rows)
        assert m["recovery"] == pytest.approx(2 / 3, abs=1e-3)
        assert m["avg_retries"] == 1.25


class TestCompareMetrics:
    def test_higher_better_regression_detected(self):
        base = {"ex": 0.80, "completion": 0.90, "avg_retries": 0.5}
        cur = {"ex": 0.70, "completion": 0.95, "avg_retries": 0.8}
        report = compare_metrics(base, cur, tolerances={"ex": "0.05"})
        by = {m.metric: m for m in report.metrics}
        # ex 下降 0.10 > 0.05 容差 → 回归
        assert by["ex"].ok is False
        assert by["ex"] in report.regressions
        # completion 上升 → OK;avg_retries 上升(lower-better)→ 回归
        assert by["completion"].ok is True
        assert by["avg_retries"].ok is False

    def test_relative_tolerance(self):
        base = {"avg_tokens": 1000.0}
        cur = {"avg_tokens": 1150.0}  # +15%
        report = compare_metrics(base, cur, tolerances={"avg_tokens": "0.10-r"})
        m = report.metrics[0]
        assert m.direction == "lower"
        assert m.ok is False  # +15% > 相对 10%
        cur2 = {"avg_tokens": 1090.0}  # +9%
        report2 = compare_metrics(base, cur2, tolerances={"avg_tokens": "0.10-r"})
        assert report2.metrics[0].ok is True

    def test_ignore_and_unpaired(self):
        base = {"ex": 0.8, "compile_hit": 0.7}
        cur = {"ex": 0.6, "mrr": 0.4}
        report = compare_metrics(base, cur, ignore={"compile_hit"})
        metrics = {m.metric: m for m in report.metrics}
        assert "compile_hit" not in metrics
        assert "mrr" in report.unpaired  # 无基线不可比

    def test_default_direction_heuristic(self):
        base = {"ex": 0.8, "avg_tokens": 500.0, "mrr": 0.3}
        cur = {"ex": 0.75, "avg_tokens": 600.0, "mrr": 0.28}
        report = compare_metrics(base, cur)
        by = {m.metric: m for m in report.metrics}
        assert by["ex"].direction == "higher"
        assert by["avg_tokens"].direction == "lower"
        assert by["mrr"].direction == "higher"
        # ex: -0.05 > 默认 0.01 容差 → 回归
        assert by["ex"].ok is False
        # avg_tokens: +20% > 默认相对 10% → 回归
        assert by["avg_tokens"].ok is False
        # mrr: -0.02 = 默认 0.02 容差,恰好不回归
        assert by["mrr"].ok is True
        assert by["mrr"].delta == -0.02

    def test_render_report(self):
        base = {"ex": 0.8, "avg_retries": 0.5}
        cur = {"ex": 0.6, "avg_retries": 1.2}
        report = compare_metrics(base, cur)
        text = render_report(report)
        assert "回归门禁" in text
        assert "REGRESS" in text
        assert "拦截" in text


class TestDenominatorStability:
    def test_n_judged_recorded(self):
        rows = [
            _eval_entry("MATCH"),
            _eval_entry("GOLD_ERROR"),  # 不进可判定
            _eval_entry("MISMATCH"),
        ]
        m = metrics_from_entries(rows)
        assert m["n"] == 3
        assert m["n_judged"] == 2

    def test_no_drift_no_note(self):
        base = {"ex": 0.8, "n_judged": 100.0}
        cur = {"ex": 0.75, "n_judged": 110.0}  # +10% < 20%
        report = compare_metrics(base, cur)
        assert report.denominator_notes == []
        assert report.passed is False  # ex 回归照常拦截

    def test_drift_warns_but_does_not_block(self):
        base = {"ex": 0.8, "n_judged": 100.0}
        cur = {"ex": 0.82, "n_judged": 60.0}  # -40% ≥ 20% 阈值
        report = compare_metrics(base, cur)
        assert len(report.denominator_notes) == 1
        assert "可判定题数漂移" in report.denominator_notes[0]
        # 告警不拦截:所有指标 OK
        assert report.passed is True
        # n_judged 本身不参与逐指标对比(纯信息量)
        assert all(m.metric != "n_judged" for m in report.metrics)

    def test_denominator_note_rendered(self):
        base = {"ex": 0.8, "n_judged": 100.0}
        cur = {"ex": 0.82, "n_judged": 40.0}
        report = compare_metrics(base, cur)
        text = render_report(report)
        assert "样本结构告警" in text
        assert "100 → 40" in text


class TestFileIO:
    def test_load_entries_skips_bad_lines(self, tmp_path):
        p = tmp_path / "r.jsonl"
        p.write_text('{"verdict": "MATCH"}\nnot-json\n{"verdict": "MISMATCH"}\n',
                     encoding="utf-8")
        assert len(load_entries(p)) == 2

    def test_scorecard_json_with_metrics(self, tmp_path):
        p = tmp_path / "score.json"
        p.write_text(json.dumps({"source": "tune_rrf", "metrics": {
            "mrr": 0.42, "recall@10": 0.9, "zero_recall": 2.0,
        }}), encoding="utf-8")
        s = score_from_file(p)
        assert s["mrr"] == 0.42
        assert s["recall@10"] == 0.9

    def test_score_from_results_file(self, tmp_path):
        p = tmp_path / "results.jsonl"
        p.write_text(json.dumps(_eval_entry("MATCH")) + "\n" +
                     json.dumps(_eval_entry("MISMATCH")) + "\n", encoding="utf-8")
        s = score_from_file(p)
        assert s["ex"] == 0.5
