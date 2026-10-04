"""效果测量 —— 行动之后,数字动了没有(纯函数,零 I/O,零 LLM)。

闭环验收(B7)的判据核心:一个行动外送了 N 天之后,把**行动期间的口径**
拿回来与行动前的历史块比一次。本模块只做算术,不问数从哪来 —— 取数在
``decision/outcome.py`` 的 verifier(包外执行面),这里不 import 任何
数据源。

三条纪律,与统计器械(stats.py)同源:

  1. **三层结局,没有第四种**。``outside_band`` = True(行动后水平超出
     行动前噪声带)/ False(行动后**无可辨识变化**)/ None(判不了)。
     False 不是失败 —— 它是最诚实也最有价值的一档:「这次行动没动到
     数」是一个**结论**;而「可能动了、但 n 不够/带退化」以 ``degraded``
     单列,绝不冒充 False(与判定侧「算不出 ≠ 在带内」同一条纪律);
  2. **带只由行动前的块构成**。把行动后的点混进分布会自我稀释 ——
     这正是「行动有效」的假阳性来源(与 ``scan/scanner.py`` 同款);
  3. **不输出 p 值**。``confidence`` 走与判定侧同一个
     ``confidence_from_margin`` 公式、同一个 ``k``(位置分数非概率,见
     decision/significance.py 的限定语)—— 于是「判定说超带」与「验收
     说超带」是同一句话,而不是两套口径下的两个相似词。

``did_2x2`` 也在这里:判定侧的因果升级梯(B3)与验收侧的净效应估计用
的是**同一个 2×2 公式**,字面同一份实现 —— 两处各写一遍迟早漂移,而
「判定看到的净效应」与「验收算出的净效应」对不上是这一层最不能出的错。
"""

from __future__ import annotations

from typing import Any, Iterable

from trove.services.analysis.stats import (
    MIN_BLOCKS,
    ROBUST_Z_THRESHOLD,
    Band,
    band,
    clean,
    confidence_from_margin,
    low_n,
    median,
    outside,
    robust_z,
)

#: 验收判带用的带宽(与判定/扫描侧同一个稳健 z 阈值):「超带」在
#: 判定与验收里因此是同一条线。
EFFECT_K = ROBUST_Z_THRESHOLD


def _f(value: Any) -> float | None:
    """宽容数值化(``None`` / 非数 → None;不抛)。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def did_2x2(
    t_pre: Any, t_post: Any, c_pre: Any, c_post: Any,
) -> dict[str, Any] | None:
    """2×2 差中差(手算公式,零模型):``ATT = (T_post−T_pre) − (C_post−C_pre)``。

    任一值缺失/非数 → ``None``(判不了,不拿 0 装数)。
    """
    t0, t1, c0, c1 = (_f(t_pre), _f(t_post), _f(c_pre), _f(c_post))
    if None in (t0, t1, c0, c1):
        return None
    return {
        "att": (t1 - t0) - (c1 - c0),
        "treated_delta": t1 - t0,
        "control_delta": c1 - c0,
    }


def interrupted_design(
    pre_blocks: Iterable[Any] | None,
    post_blocks: Iterable[Any] | None,
    *,
    k: float = EFFECT_K,
    min_n: int = MIN_BLOCKS,
) -> dict[str, Any]:
    """ITS 核心量 —— 行动前分布 → 行动后水平(纯,给不带判定的一半)。

    ``pre`` = 行动前的历史块(**只由它们建带**),``post`` = 行动后的块
    (验收路径通常只有一个:规则自己那一个期间的当期值)。中心量一律
    取 **median**(与噪声带的 center 同口径 —— 均值会被单点拉走,而
    「行动后水平」恰恰是要抗单点的那一侧)。

    ``delta`` = post 中心 − pre 中心;``pct`` = 相对量(**小数口径**,
    与判定卡的 ``delta_pct`` 一致,0.25 = +25%)。pre 中心为 0 →
    ``pct=None`` 且 ``degraded`` 记 ``zero_baseline``(相对零没有百分比
    这回事,不发明分母)。
    """
    pre = clean(pre_blocks)
    post = clean(post_blocks)
    b = band(pre, k=k, min_n=min_n)
    degraded = list(b.degraded)
    if not pre:
        degraded.append("no_pre_blocks")
    if not post:
        degraded.append("no_post_blocks")

    pre_center = median(pre)
    post_center = median(post)
    delta: float | None = None
    pct: float | None = None
    z: float | None = None
    if pre_center is not None and post_center is not None:
        delta = post_center - pre_center
        if pre_center != 0:
            pct = delta / abs(pre_center)
        else:
            degraded.append("zero_baseline")
        z = robust_z(post_center, pre)

    return {
        "method": "its",
        "pre": {"n": len(pre), "center": pre_center, "scale": b.scale},
        "post": {"n": len(post), "center": post_center},
        "delta": delta,
        "pct": pct,
        "z": z,
        "band": b.to_dict(),
        "k": float(k),
        "low_n": low_n(pre),
        "degraded": degraded,
    }


def measure_effect(
    pre_blocks: Iterable[Any] | None,
    post_blocks: Iterable[Any] | None,
    band: Band | dict[str, Any] | None = None,
    *,
    k: float = EFFECT_K,
    min_n: int = MIN_BLOCKS,
) -> dict[str, Any]:
    """ITS 核心量 + 带判定 → 完整效果记录(纯)。

    ``band`` = 噪声带(``Band`` 或它的 ``to_dict`` 形状);缺省用
    ``pre_blocks`` 现建一个 —— 显式传入的意义是**可复算**:带可以从
    别处(存下来的证据)原样拿回来再判,判的是同一条带。

    ``outside_band`` 的三值语义见模块 docstring;``confidence`` 只在带
    判定得出时给(位置分数非概率)。
    """
    body = interrupted_design(pre_blocks, post_blocks, k=k, min_n=min_n)
    if isinstance(band, Band):
        b = band
    else:
        b = Band.from_dict(band if isinstance(band, dict) else body["band"])
    post_center = body["post"]["center"]
    outside_band = outside(post_center, b) if post_center is not None else None
    conf = confidence_from_margin(body["z"], k)
    body["outside_band"] = outside_band
    body["confidence"] = conf if outside_band is not None else None
    return body


def attribute_effect(
    effect: dict[str, Any], *,
    treated_pre: Any, treated_post: Any,
    control_pre: Any, control_post: Any,
) -> dict[str, Any]:
    """给效果记录附上净效应(2×2 DiD)—— **绝不改动 ITS 的数字**。

    四值齐备 → ``causal="did"``、``att={att, treated_delta, control_delta}``、
    ``method`` 升为 ``"its+did"``(方法名描述的是**最强的那层主张**,
    ``delta``/``pct`` 仍然来自 ITS —— 两者不是同一件事,方法名把它们
    区分开);缺任一值 → ``causal="unavailable"`` + ``causal_reason``,
    ITS 结论原样保留(对照不可用是验收侧最常见的形态,不是错误)。
    """
    out = dict(effect)
    out["degraded"] = list(effect.get("degraded") or [])
    did = did_2x2(treated_pre, treated_post, control_pre, control_post)
    if did is None:
        out["causal"] = "unavailable"
        out["causal_reason"] = "control_incomplete"
        out["att"] = None
        out["degraded"].append("causal:control_incomplete")
        return out
    out["causal"] = "did"
    out["causal_reason"] = ""
    out["att"] = did
    out["method"] = "its+did"
    return out
