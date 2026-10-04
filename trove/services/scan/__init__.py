"""主动扫描 + 假设层(B6)。

确定性的「谁超出了历史噪声带」扫描(``scanner``)、把发现落成待确认的
决策规则草稿(``service``),以及一轮可选的 LLM 假设 → 确定性裁决
(``hypotheses``)。LLM 只在假设层出现,且只负责**起草候选**;一条假设
支不支持由数据按闭集规则判,模型自己不判自己。

本包是**消费方**:只读地使用 ``analysis``(块序列/噪声带/编译 hop)与
``decision``(规则草稿门),绝不 import 数据源适配器 / KB 服务 / jobs ——
见 ``tests/services/scan/test_scan_readonly_posture.py``。
"""

from __future__ import annotations

from trove.services.scan.models import (
    DIRECTIONS,
    FINDING_KINDS,
    HYPOTHESIS_STATUSES,
    SCAN_MODES,
    Finding,
    Hypothesis,
    ScanError,
    ScanSpec,
    VerifiedHypothesis,
)

__all__ = [
    "DIRECTIONS",
    "FINDING_KINDS",
    "HYPOTHESIS_STATUSES",
    "SCAN_MODES",
    "Finding",
    "Hypothesis",
    "ScanError",
    "ScanSpec",
    "VerifiedHypothesis",
]
