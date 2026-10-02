"""离线评测回归门测试:指标计算 + 方向/容差判定 + 渲染(纯函数,零 IO)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

from trove.eval.gate import (
    DEFAULT_TOLERANCE,
    HIGHER_BETTER,
    compare_metrics,
    load_entries,
    metrics_from_entries,
    paired_token_stats,
    render_report,
    score_from_file,
)
from trove.eval.replay import EX_PATH_TIERS, score_replay, scorecard_metrics


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

    def test_new_metrics_self_consistency_zero_answer_first_pass(self):
        rows = [
            _eval_entry("MATCH", n_candidates=5),             # 一次通过 + 自洽
            _eval_entry("MATCH", retries=1, n_candidates=5),  # 恢复后才对
            _eval_entry("EMPTY_SQL", pred_sql=""),            # 零交付且没重试
        ]
        m = metrics_from_entries(rows)
        assert m["self_consistency"] == pytest.approx(2 / 3, abs=1e-3)
        assert m["zero_answer"] == pytest.approx(1 / 3, abs=1e-3)
        assert m["first_pass"] == pytest.approx(1 / 3, abs=1e-3)


class TestMetricParity:
    """两引擎(score_replay 与 metrics_from_entries)在同一份条目上必须逐项相等。

    基线由 score_replay 钉、CI 由 metrics_from_entries 比 —— 两边漂移的后果
    是门红得莫名其妙(或该红时不红)。2026-10 实测过的漂移:同一份冻结文件
    completion 0.9375 vs 0.9062、recovery 1.0 vs 0.1765。
    """

    #: 两引擎共享、必须逐项相等的键(CI 对比的就是这些)。``ex_by_path:*``
    #: 是**键族通配**:分档 EX 的档位集合与取值两侧必须逐项相同 —— 任一侧
    #: 漏掉一档(最容易漏的正是 refused:拒绝行 path 非空、verdict 可判定,
    #: 匹配不到档就从分档视图里静默消失)都在这条断言上现形。
    SHARED = (
        "ex", "completion", "self_consistency", "recovery", "zero_answer",
        "consensus_rate", "avg_confidence", "first_pass", "gold_match",
        "n", "n_judged", "ex_by_path:*",
    )

    @staticmethod
    def _rows():
        return [
            _eval_entry("MATCH", n_candidates=5, gold_sql="SELECT 1", path="compiled"),
            _eval_entry("MATCH", retries=2, n_candidates=3, path="partial"),
            _eval_entry("MISMATCH", retries=1, n_candidates=5, path="llm"),
            _eval_entry("EXECUTION_ERROR", retries=2, n_candidates=5, path="llm"),
            _eval_entry("EMPTY_SQL", pred_sql="", n_candidates=0, path="llm"),
            _eval_entry("REFUSED", pred_sql="", n_candidates=0, path="refused"),
            _eval_entry("GOLD_ERROR", n_candidates=2, path="llm"),
        ]

    def _assert_parity(self, rows):
        gate_m = metrics_from_entries(rows)
        replay_m = scorecard_metrics(score_replay(rows))
        for key in self.SHARED:
            if key.endswith(":*"):
                prefix = key[:-1]
                got = {k: v for k, v in gate_m.items() if k.startswith(prefix)}
                want = {k: v for k, v in replay_m.items() if k.startswith(prefix)}
                assert got == want, f"{key}: 门 {got} != 回放 {want}"
                continue
            assert key in gate_m, f"门侧缺 {key}"
            assert key in replay_m, f"回放侧缺 {key}"
            assert gate_m[key] == replay_m[key], (
                f"{key}: 门 {gate_m[key]} != 回放 {replay_m[key]}"
            )

    def test_engines_agree_on_shared_metrics(self):
        self._assert_parity(self._rows())

    def test_engines_agree_on_the_frozen_baseline_file(self):
        """真文件上也要相等 —— 人造夹具漂移常在真实条目形状下才现形
        (双 retry 键名、缺 tokens、缺 confidence)。"""
        rows = load_entries(_ROOT / "eval/baseline/results.jsonl")
        assert rows, "冻结基线缺失"
        self._assert_parity(rows)

    def test_refused_has_its_own_tier_on_both_engines(self):
        """档位清单单点定义(replay.EX_PATH_TIERS),refused 不许在任一侧缺席。

        拒绝题没有 MATCH 可言 → 该档 EX 恒 0,但**必须可见**:看不见拒绝,
        "拒绝率上升"就只会表现为别的档分母变小,准确率看着变好。
        """
        assert EX_PATH_TIERS == ("compiled", "partial", "llm", "refused")
        rows = [
            _eval_entry("MATCH", path="compiled"),
            _eval_entry("MISMATCH", path="partial"),
            _eval_entry("MISMATCH", path="llm"),
            _eval_entry("REFUSED", pred_sql="", path="refused"),
        ]
        gate_m = metrics_from_entries(rows)
        replay_m = scorecard_metrics(score_replay(rows))
        expected = {
            "ex_by_path:compiled": 1.0,
            "ex_by_path:partial": 0.0,
            "ex_by_path:llm": 0.0,
            "ex_by_path:refused": 0.0,
        }
        assert {k: v for k, v in gate_m.items() if k.startswith("ex_by_path:")} == expected
        assert {k: v for k, v in replay_m.items() if k.startswith("ex_by_path:")} == expected

    def test_cache_metrics_paired_or_absent(self):
        """cache_hit_rate 不进 SHARED(冻结基线无 cache 键,进了必红),
        但两侧必须**同时缺席/同时相等** —— 单侧发键会让门与基线错位。"""
        # 默认条目无 tokens → 两侧都不发键
        rows = self._rows()
        gate_m = metrics_from_entries(rows)
        replay_m = scorecard_metrics(score_replay(rows))
        assert "cache_hit_rate" not in gate_m
        assert "cache_hit_rate" not in replay_m

        # 有测量 → 两侧同时发且相等(90/100 + 30/300 = 120/400)
        rows2 = [
            _eval_entry("MATCH", tokens={"prompt": 100, "completion": 10,
                                         "total": 110, "cached_tokens": 90}),
            _eval_entry("MISMATCH", tokens={"prompt": 300, "completion": 10,
                                            "total": 310, "cached_tokens": 30}),
        ]
        gate_m2 = metrics_from_entries(rows2)
        replay_m2 = scorecard_metrics(score_replay(rows2))
        assert gate_m2["cache_hit_rate"] == 0.3
        assert replay_m2["cache_hit_rate"] == 0.3


def _tok_entry(key: str, total: int) -> dict:
    """带 token 录制的配对条目(total=0 → tokens 空 = 未录制)。"""
    return {"qid": key, "question": key, "tokens": {"total": total} if total else {}}


class TestTokenDenominatorVisibility:
    """token 均值的分母可见性:覆盖数随行携带 + 覆盖不同时显式告警。

    2026-10 实测的口径假象:冻结基线 32 条只有 19 条录了 tokens、现值
    32/32,gate 报 avg_tokens −38%,同 19 题配对真值 −20.3%。值不改
    (改了门与基线就错位),要改的是"不可直接比"这件事的可见性。
    """

    #: 实测形状:两侧覆盖 19 vs 32
    BASE = {"avg_tokens": 90555.5, "avg_tokens_n": 19.0, "total_tokens": 1720554.0}
    CUR = {"avg_tokens": 56078.4, "avg_tokens_n": 32.0, "total_tokens": 1794509.0}

    def test_avg_tokens_value_and_denominator_unchanged(self):
        """旧行为不回归:分母仍是"本方有 token 数据的条目",未录制的条目
        不补零、不进分母;新增的只是覆盖计数。"""
        rows = [
            _eval_entry("MATCH", tokens={"total": 100}),
            _eval_entry("MATCH", tokens={"total": 300}),
            _eval_entry("MATCH"),  # 未录制 tokens
        ]
        m = metrics_from_entries(rows)
        assert m["avg_tokens"] == 200  # 400/2,不是 400/3
        assert m["total_tokens"] == 400
        assert m["avg_tokens_n"] == 2.0
        # 与回放引擎逐项同值(值不动的跨引擎确认)
        assert scorecard_metrics(score_replay(rows))["avg_tokens"] == m["avg_tokens"]

    def test_coverage_gap_noted_with_both_counts(self):
        report = compare_metrics(self.BASE, self.CUR)
        assert len(report.denominator_notes) == 1
        note = report.denominator_notes[0]
        assert "n=19" in note and "n=32" in note
        assert "基线缺 13 条" in note and "不可直接比" in note
        # 告警不拦截:均值 −38% 是下降(成本更低),不是回归
        assert report.passed is True

    def test_count_key_never_judged(self):
        """覆盖计数是分母元信息(同 n/n_judged):不成指标行、不进不可比。"""
        report = compare_metrics(self.BASE, self.CUR)
        assert all(m.metric != "avg_tokens_n" for m in report.metrics)
        assert "avg_tokens_n" not in report.unpaired

    def test_render_shows_counts_in_row_and_warning(self):
        text = render_report(compare_metrics(self.BASE, self.CUR))
        assert "[覆盖 n=19→32]" in text     # 数字旁(报告行)
        assert "token 均值覆盖不同" in text  # ⚠ 样本结构告警段

    def test_no_note_when_coverage_matches(self):
        report = compare_metrics(dict(self.BASE, avg_tokens_n=32.0), self.CUR)
        assert report.denominator_notes == []
        assert "[覆盖" not in render_report(report)

    def test_ignore_avg_tokens_silences_coverage_note(self):
        report = compare_metrics(self.BASE, self.CUR, ignore={"avg_tokens"})
        assert report.denominator_notes == []
        assert all(m.metric != "avg_tokens" for m in report.metrics)

    def test_missing_counts_no_note(self):
        """旧 scorecard 快照没有覆盖计数 → 无从判断,不猜、不告警。"""
        report = compare_metrics({"avg_tokens": 100.0}, {"avg_tokens": 90.0})
        assert report.denominator_notes == []

    def test_frozen_baseline_value_unchanged_and_coverage_visible(self):
        """冻结基线:值与钉住的 scorecard 逐项同,覆盖缺口(19/32)可见。"""
        rows = load_entries(_ROOT / "eval/baseline/results.jsonl")
        assert rows, "冻结基线缺失"
        pinned = json.loads(
            (_ROOT / "eval/baseline/scorecard.json").read_text(encoding="utf-8")
        )["metrics"]
        m = metrics_from_entries(rows)
        assert m["avg_tokens"] == pytest.approx(pinned["avg_tokens"], abs=0.05)
        assert m["total_tokens"] == pinned["total_tokens"]
        assert m["avg_tokens_n"] < m["n"] == 32.0
        assert m["avg_tokens_n"] == 19.0  # 实测:基线 32 条只有 19 条带 tokens
        report = compare_metrics(m, m)
        assert "[覆盖" not in render_report(report)  # 同覆盖 → 不误报


class TestPairedTokenStats:
    """配对口径 token 均值:只在两侧都有 token 数据的**交集条目**上算。"""

    def test_only_intersection_counted(self):
        base = [
            _tok_entry("q1", 100), _tok_entry("q2", 200), _tok_entry("q3", 300),
            _tok_entry("q4", 0),  # 未录制 token → 不参与
        ]
        cur = [
            _tok_entry("q1", 50), _tok_entry("q2", 100), _tok_entry("q3", 150),
            _tok_entry("q4", 9999), _tok_entry("q5", 8888),
        ]
        stats = paired_token_stats(base, cur)
        assert stats["n_paired"] == 3
        assert stats["baseline_avg_tokens"] == 200.0
        assert stats["current_avg_tokens"] == 100.0   # q4/q5 的巨额绝不被摊进来
        assert stats["delta_pct"] == -50.0
        assert stats["n_baseline_with_tokens"] == 3
        assert stats["n_current_with_tokens"] == 5
        assert stats["n_baseline_unpaired"] == 0
        assert stats["n_current_unpaired"] == 2      # q4/q5 如实报未配对

    def test_no_intersection_returns_none(self):
        assert paired_token_stats([_tok_entry("q1", 100)], [_tok_entry("zz", 5)]) is None
        assert paired_token_stats([], []) is None
        # 一侧有 token、另一侧同题未录制 → 交集为空(不是 0 均值)
        assert paired_token_stats([_tok_entry("q1", 100)], [_tok_entry("q1", 0)]) is None

    def test_pairing_falls_back_to_question_text(self):
        """一侧带 qid、另一侧只有问题文本时仍能对上(对账键同 baseline)。"""
        base = [{"qid": "financial-0001", "question": "How  Many ACCOUNTS?",
                 "tokens": {"total": 10}}]
        cur = [{"question": "how many accounts?", "tokens": {"total": 20}}]
        stats = paired_token_stats(base, cur)
        assert stats["n_paired"] == 1
        assert stats["delta_pct"] == 100.0

    def test_duplicate_keys_pair_in_order(self):
        base = [{"qid": "q1", "tokens": {"total": 10}},
                {"qid": "q1", "tokens": {"total": 20}}]
        cur = [{"qid": "q1", "tokens": {"total": 1}},
               {"qid": "q1", "tokens": {"total": 2}}]
        stats = paired_token_stats(base, cur)
        assert stats["n_paired"] == 2
        assert stats["baseline_avg_tokens"] == 15.0
        assert stats["current_avg_tokens"] == 1.5

    def test_question_norm_matches_baseline_reconciliation_key(self):
        """配对键与 baseline 对账键同一口径(两处各写一遍必然漂移)。"""
        from trove.eval.baseline import _norm
        from trove.eval.gate import _norm_question

        for text in ("How  many ACCOUNTS?", "  total-balance_by district ", ""):
            assert _norm_question(text) == _norm(text)

    def test_stats_render_as_their_own_section(self):
        base = [_tok_entry("q1", 100), _tok_entry("q2", 200)]
        cur = [_tok_entry("q1", 50), _tok_entry("q2", 100), _tok_entry("q3", 1)]
        report = compare_metrics(
            {"avg_tokens": 150.0, "avg_tokens_n": 2.0},
            {"avg_tokens": 50.3, "avg_tokens_n": 3.0},
        )
        report.token_pairing = paired_token_stats(base, cur)
        text = render_report(report)
        assert "配对口径 token 均值" in text
        assert "交集 2 题" in text
        assert "150.0 → 75.0(Δ -50.0%)" in text
        assert "未配对:基线 0 条 / 现值 1 条" in text


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

    def test_new_metric_directions(self):
        report = compare_metrics(
            {"self_consistency": 0.50, "first_pass": 0.50, "zero_answer": 0.10},
            {"self_consistency": 0.40, "first_pass": 0.45, "zero_answer": 0.20},
        )
        by = {m.metric: m for m in report.metrics}
        assert by["self_consistency"].direction == "higher"
        assert by["self_consistency"].ok is False   # -0.10 > 容差 0.01
        assert by["first_pass"].direction == "higher"
        assert by["first_pass"].ok is False         # -0.05 > 容差 0.01
        assert by["zero_answer"].direction == "lower"
        assert by["zero_answer"].ok is False        # +0.10 > 容差 0.02
        # zero_answer 上升 0.01 在容差内
        assert compare_metrics(
            {"zero_answer": 0.10}, {"zero_answer": 0.11}
        ).metrics[0].ok is True

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

    def test_cache_hit_rate_direction_and_tolerance(self):
        """缓存命中率 = 越高越好,默认相对容差 20%(抖动期可 --ignore)。"""
        assert "cache_hit_rate" in HIGHER_BETTER
        assert DEFAULT_TOLERANCE["cache_hit_rate"] == "0.20-r"

        m = compare_metrics({"cache_hit_rate": 0.5}, {"cache_hit_rate": 0.3}).metrics[0]
        assert m.direction == "higher"
        assert m.ok is False  # -0.20 > 0.5*0.20 阈值
        # -0.05 在相对 20% 内 → 不拦
        assert compare_metrics(
            {"cache_hit_rate": 0.5}, {"cache_hit_rate": 0.45}
        ).metrics[0].ok is True
        # 未测量一侧(键缺席)→ 不可比,进 unpaired 而不是当 0
        report = compare_metrics({"cache_hit_rate": 0.5}, {})
        assert "cache_hit_rate" in report.unpaired

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
