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

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PromotionEvidence:
    """One nudge toward promotion; ``kind`` drives the delta."""

    kind: str          # lesson_reuse_pass | upvote | repeated_correction
    note: str = ""
    delta: float = 0.0


# 每次支持证据的置信度增量(经验值,可调)。
_EVIDENCE_DELTA: dict[str, float] = {
    "lesson_reuse_pass": 0.25,
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


@dataclass
class PromotionLedger:
    """进程内证据账本(可选;也可直接从 lessons.yml 读 confidence)。"""

    by_key: dict[str, list[PromotionEvidence]] = field(default_factory=dict)

    def bump(self, key: str, evidence: PromotionEvidence) -> float:
        entries = self.by_key.setdefault(key, [])
        entries.append(evidence)
        conf = 0.0
        for e in entries:
            conf = apply_evidence(conf, e.kind, len([
                x for x in entries if x.kind == e.kind
            ]))
        return conf
