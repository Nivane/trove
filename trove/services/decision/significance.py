"""显著性/噪声带 —— 判定触发的第二道门(纯函数,零 LLM,零 I/O)。

判定的条件 DSL 回答「变化大不大」(对比基期),但**「大」是相对于
什么的**?两个数据点的对比没有噪声概念:基期恰好偏低,任何回升都
像「显著」。这个模块给触发加一道确定性门:**当期值是否超出历史同
粒度块的噪声带**(主判据 = 稳健 z,阈值 k 默认 3.5)。

三条口径,每一条都有对应测试:

  1. **算不出 ≠ 在带内**。带不可用(块数不足 / 无散布 / 无数据)时
     ``gate`` 返回 ``outside=None``、``gated=False`` 且 ``reason`` 写明
     原因 —— 判定侧 ``require: outside_band`` 会把「算不出」升级成
     error run(不静默放行),这层语义在 service,不在这里;
  2. **不输出 p 值**。n≈8–12 上的 p 是伪精度,比没有更糟;并列证据
     只有稳健 z、bootstrap 分位区间、经验秩位置 —— ``confidence`` 是
     **位置分数非概率**((|z|−k)/k 截断到 [0,1]),文档与证据里都
     写明,不许被读成置信度;
  3. **明标「位置分数非概率」**。字段名沿用业界直觉(confidence),
     但每一个出口(证据节 note、通知行)都带着限定语。
"""

from __future__ import annotations

from typing import Any, Iterable

from trove.services.analysis.stats import (
    MIN_BLOCKS,
    ROBUST_Z_THRESHOLD,
    Band,
    band,
    bootstrap_ci,
    confidence_from_margin,
    effective_n,
    low_n,
    outside,
    robust_z,
)

#: 本模块的公开出口(B7 起 ``confidence_from_margin`` 的公式本体搬到
#: ``analysis/stats.py`` —— 验收侧要用同一个公式,而 analysis 不得
#: 反向依赖 decision;这里 re-export,既有 import 点逐字不变)。
__all__ = [
    "CONFIDENCE_NOTE",
    "band_line",
    "build_band_payload",
    "confidence_from_margin",
    "gate",
]

#: 证据节与字段文档共用的限定语 —— 出口处必须出现,防止被读成概率。
CONFIDENCE_NOTE = (
    "位置分数非概率:(|z|−k)/k 截断到 [0,1],0 = 恰在带缘、1 = 两倍阈值;"
    "无分布假设,不输出 p 值。"
)

#: 「判不了」的 band.degraded 原因(带不可用的硬原因;insufficient_n 另算:
#: 带可能算得出,但块数不足时**不予确认**,见 gate)。
_HARD_DEGRADED = ("no_data", "zero_scale")


def build_band_payload(
    values: Iterable[Any] | None,
    *,
    k: float = ROBUST_Z_THRESHOLD,
    min_n: int = MIN_BLOCKS,
    seed_material: str = "",
) -> dict[str, Any]:
    """历史块值 → 噪声带证据节(band + bootstrap 分位 + n_eff + low_n)。

    只消费**历史**块(被测窗口不参与 —— 混入会自我稀释,见 series.py);
    bootstrap 的 ``seed_material`` 由调用方钉住语境(如
    ``f"{datasource}|{metric}|{dim}|{blocks[0][0]}"``),同一材料复算出
    同一区间。
    """
    b = band(values, k=k, min_n=min_n)
    return {
        "band": b.to_dict(),
        "k": float(k),
        "bootstrap": bootstrap_ci(values, seed_material=seed_material),
        "n_eff": effective_n(values),
        "low_n": low_n(values),
    }


def gate(
    values: Iterable[Any] | None,
    x: Any,
    payload: dict[str, Any] | None,
    *,
    min_confidence: float = 0.0,
) -> dict[str, Any]:
    """当期值 × 噪声带 payload → ``{z, outside, confidence, gated, reason}``。

    ``gated=True`` 仅当:带可用(且有数据) ∧ 块数 ≥ MIN_BLOCKS(硬门:
    块数不足时显著性会变成噪声放大器,宁可不确认) ∧ 落在带外 ∧
    位置分数 ≥ ``min_confidence``。其余一律 False + 原因 ——
    **「判不了」与「判了在带内」用 reason 区分,不混为一谈**。
    """
    payload = payload if isinstance(payload, dict) else {}
    raw_band = payload.get("band")
    b = Band.from_dict(raw_band if isinstance(raw_band, dict) else None)
    try:
        k = float(payload.get("k") or ROBUST_Z_THRESHOLD)
    except (TypeError, ValueError):
        k = ROBUST_Z_THRESHOLD

    if x is None:
        return {"z": None, "outside": None, "confidence": None,
                "gated": False, "reason": "no_current_value"}
    if int(b.n) == 0 and not b.degraded:
        return {"z": None, "outside": None, "confidence": None,
                "gated": False, "reason": "no_band"}

    z = robust_z(x, values)
    out = outside(x, b)
    conf = confidence_from_margin(z, k)

    degraded = list(b.degraded)
    if out is None:
        reason = next((d for d in degraded if d in _HARD_DEGRADED),
                      "band_unavailable")
    elif "insufficient_n" in degraded:
        reason = "insufficient_n"
    elif out is False:
        reason = "within_band"
    elif conf is None:
        reason = "confidence_unavailable"
    elif conf < float(min_confidence):
        reason = "below_min_confidence"
    else:
        return {"z": z, "outside": True, "confidence": conf,
                "gated": True, "reason": ""}
    return {"z": z, "outside": out, "confidence": conf,
            "gated": False, "reason": reason}


def _fmt2(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def band_line(section: dict[str, Any] | None) -> str:
    """significance 证据节 → 一行通知缀(没有已确认的组 → 空串)。

    只列 ``gated=True`` 的组(≤2 个,按 |z| 降序、dim 升序 —— 确定性),
    每个组给「值超带了多少」:|z| 与阈值 k。位置分数只在非 None 时缀上,
    行内保持一行可读;限定语(非概率)在证据节,不在通知里。
    """
    by_dim = (section or {}).get("by_dim")
    if not isinstance(by_dim, dict):
        return ""
    gated = [
        (dim, entry) for dim, entry in by_dim.items()
        if isinstance(entry, dict) and entry.get("gated") is True
    ]
    if not gated:
        return ""
    gated.sort(key=lambda it: (-abs(it[1].get("z") or 0.0), it[0]))
    parts: list[str] = []
    for dim, entry in gated[:2]:
        k = entry.get("k")
        prefix = f"{dim} " if dim else ""
        bits = f"{prefix}|z|={_fmt2(entry.get('z'))} > k={_fmt2(k)}"
        conf = entry.get("confidence")
        if conf is not None:
            bits += f"（位置 {_fmt2(conf)}）"
        parts.append(bits)
    return "噪声带：" + "；".join(parts)
