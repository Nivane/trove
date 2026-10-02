"""钉住的基线快照必须能从 committed 产物复算出来。

CI 的回归门把 ``eval/baseline/scorecard.json`` 当基线,却没有任何单测保证
"钉住的快照"与"引擎现在算的"一致 —— 一旦脱钩(改名/改口径/换 emit 路径),
``compare_metrics`` 把两侧都算成 unpaired:报告里多一行"无基线不可比",
门却照样绿。这个测试是那条静默通道的闸:

- 重钉(换基线)必须真的经 ``scorecard_metrics`` 落地,不许手改 JSON;
- 改名(correctness → self_consistency 那类)要么同步重钉,要么这里先红。
"""

from __future__ import annotations

import json
from pathlib import Path

from trove.eval.replay import load_entries, score_replay, scorecard_metrics

_ROOT = Path(__file__).resolve().parents[2]
_SCORECARD = _ROOT / "eval/baseline/scorecard.json"
_RESULTS = _ROOT / "eval/baseline/results.jsonl"


def _pinned() -> dict:
    return json.loads(_SCORECARD.read_text(encoding="utf-8"))


def test_pinned_scorecard_matches_recomputed_metrics():
    entries = load_entries(_RESULTS)
    assert entries, "冻结基线缺失:eval/baseline/results.jsonl 不存在或为空"
    recomputed = scorecard_metrics(score_replay(entries))
    assert recomputed == _pinned()["metrics"]


def test_pinned_scorecard_has_no_dead_metric_names():
    """旧名不许复活:新旧两键并存时,旧键在门两侧永远 unpaired。"""
    assert "correctness" not in _pinned()["metrics"]


def test_pinned_scorecard_carries_the_understanding_metrics():
    """一次通过率/零交付率必须真的钉在基线里(它们是 #1 的验收面)。"""
    metrics = _pinned()["metrics"]
    assert "first_pass" in metrics
    assert "zero_answer" in metrics
    assert metrics["n_judged"] > 0
