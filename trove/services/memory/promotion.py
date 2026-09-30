"""Auto-promotion — pending memory → confirmed by accumulated evidence.

Lessons/examples today stay pending until an admin confirms them, so the
Hint Bank grows only as fast as a human reviews. This module adds a
confidence accumulator: every piece of supporting evidence (a reused lesson
that passed, an upvote, a repeated-correction success) nudges ``confidence``
toward the promotion threshold; crossing it auto-confirms. The threshold
defaults conservative and the whole promotion channel is opt-in
(``agent.memory.promotion: true``).

Persistence stays in the same YAML files (``lessons.yml``/``examples.yml``)
with additive fields (``confidence``/``source``/``evidence``) so manual
review and audit are unchanged — auto-confirmed items are still visible and
revertible by an admin.
"""

from __future__ import annotations

from typing import Any


# 每次支持证据的置信度增量(经验值,可调)。
# 只登记**真的有产出方**的证据:upvote 来自 rate_lesson(api/routers/kb.py),
# repeated_correction 来自会话里的反复纠正(session.py)。曾有一条
# lesson_reuse_pass(教训被复用且当轮成功)—— 没有任何代码产生这种证据,且
# 与 repeated_correction 在语义上重叠(同一件事可能加两次分),已删。
_EVIDENCE_DELTA: dict[str, float] = {
    "upvote": 0.4,
    "repeated_correction": 0.3,
}

# 需要凑齐的"独立证据次数"用于反复修正晋升(减少单次误判)。
REPEATED_CORRECTION_MIN = 2


def evidence_delta(kind: str) -> float:
    return _EVIDENCE_DELTA.get(kind, 0.0)


def apply_evidence(confidence: float, kind: str, count: int = 1) -> float:
    """累加证据增量(封顶 1.0)。"""
    if kind == "repeated_correction" and count < REPEATED_CORRECTION_MIN:
        return confidence
    delta = evidence_delta(kind)
    return max(0.0, min(1.0, confidence + delta * count))


def maybe_promote(lesson: dict[str, Any], threshold: float) -> bool:
    """是否达到自动晋升:置信度过阈值 或 净好评达到 3。"""
    confidence = float(lesson.get("confidence") or 0.0)
    net_votes = int(lesson.get("upvotes") or 0) - int(lesson.get("downvotes") or 0)
    return confidence >= threshold or net_votes >= 3
