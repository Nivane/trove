"""因果升级梯 —— L1 确定性分解 / L2 差中差 / L3 合成对照(纯,零 LLM,零 I/O)。

判定的条件 DSL 与显著性门回答「变化大不大、是否超出噪声带」;它们都
**不回答「为什么」**。归因桥给出确定性分解(加性恒等式/驱动器树),那是
**描述**而不是因果主张:分解说「华东贡献了 -92%」,不说「是华东导致了
变化」。

本模块是一条**条件式升级梯**:默认停在 L1(确定性分解,不主张因果);
只有清单上的条件逐条可判定且全部满足,才升级到能给出反事实估计的
设计 —— L2 差中差(DiD)、L3 合成对照(无优化器版)。三条铁律:

  1. **条件满足才升级,不满足诚实降级回 L1** —— ``ladder_decision``
     返回 ``unmet`` 清单,**每一条都带实测值与阈值**(验收要看得见
     「差多少才算过」),而不是一句「条件不足」;
  2. **算不出 ≠ 平行** —— placebo 前窗检验做不出(k 对不够 / 量级为 0)
     时**不升级**,理由单列(与显著性门「算不出 ≠ 在带内」同一条纪律);
  3. **假设清单非空,且「无法检验」写出来而不是省略** —— 每条结论都带
     ``assumptions`` 数组,逐条附 ``checked``(True / None=无法由数据
     检验);``causes 未受同一干预影响`` 这类不可检验的假设必须出现且
     ``checked=None``。

统计口径(与 ``decision/significance.py`` 同款纪律):

  - 零统计依赖(statistics/math 之外无第三方),可复算 ——
    verdict 是不可编辑的审计记录,反事实估计必须能从证据里的数字
    复算出同一结果;
  - **不输出 p 值**。DiD 的 2×2 是四个数、没有残差自由度,唯一的
    噪声估计是 placebo 漂移的散布 —— ``did_se`` 明标「placebo 漂移的
    标准差(粗噪声估计),非模型标准误」,``crosses_zero`` 用 ±2·se
    的朴素带,不冒充置信区间;
  - 分类量与线性形式(L3 的等权供体平均 + 比值外推)是刻意的最简
    可解释版本 —— 优化器版(权重合成)超出零依赖边界,不做。

这个模块**绝不改变 triggered** —— 因果是附录不是前置:调用方(判定
侧 ``_causal_stage``)只把结果写进证据与消息缀,**任何失败都降级为
``degraded``,绝不上抛**。
"""

from __future__ import annotations

from statistics import fmean
from typing import Any, Sequence

from trove.services.analysis.effect import did_2x2
from trove.services.analysis.stats import MIN_BLOCKS

#: 公式本体在 ``analysis/effect.py``(B7 搬下去):判定侧的升级梯与验收
#: 侧的净效应估计**必须是字面同一份 2×2** —— 两处各写一遍,「判定看到
#: 的净效应」与「验收算出的净效应」迟早对不上,而那是这一层最不能出的
#: 错。这里 re-export,既有 import 点(service._causal_stage 与测试)不变。
__all__ = [
    "CAUSAL_NOTE",
    "CROSSES_ZERO_BAND",
    "DEFAULT_CAUSAL_MODE",
    "DEFAULT_PLACEBO_BLOCKS",
    "DEFAULT_TOLERANCE",
    "LADDER_ASSUMPTIONS",
    "assumptions_for",
    "causal_line",
    "crosses_zero",
    "did_2x2",
    "did_se",
    "donor_counterfactual",
    "ladder_decision",
    "parallel_trends",
    "placebo_pairs",
]

#: ``causal.mode`` 默认值(闭集在 ``rules.CAUSAL_MODES``)。
DEFAULT_CAUSAL_MODE = "auto"
#: ``causal.placebo_blocks`` 默认值 —— 前窗相邻块对的检验对数。
DEFAULT_PLACEBO_BLOCKS = 4
#: ``causal.tolerance`` 默认值 —— placebo 漂移与合成拟合共用的相对容差
#: (相对前窗水平量级;两条检验的量纲都是「前窗 index 偏差」)。
DEFAULT_TOLERANCE = 0.1
#: 效应「跨零」判定的带宽倍数(±2·se;se = placebo 漂移标准差)。
CROSSES_ZERO_BAND = 2.0

#: 证据节与消息缀共用的限定语 —— 出口必须出现,防止被读成实验结论。
CAUSAL_NOTE = (
    "净效应是条件式反事实估计(升级梯逐条检验,见 assumptions),"
    "不是实验结论;不进入触发判定。"
)

#: 每一级的假设清单:``(文本, 可由数据检验)``。不可检验的条目
#: ``checked=None`` 出现而不是省略 —— 「无法检验」本身就是审计事实。
LADDER_ASSUMPTIONS: dict[str, tuple[tuple[str, bool], ...]] = {
    "L1": (
        ("只做确定性贡献分解(加性恒等式),不主张因果。", False),
    ),
    "L2": (
        ("处理组与对照组的差中差在干预前保持平行(placebo 前窗检验)。", True),
        ("前窗与当期同粒度、同口径,且期间口径未变。", True),
        ("对照单元未受同一干预影响(数据无法检验)。", False),
    ),
    "L3": (
        ("处理组与合成对照的差中差在干预前保持平行(placebo 前窗检验)。", True),
        ("供体池在前窗与处理组同步(拟合诊断)。", True),
        ("前窗与当期同粒度、同口径,且期间口径未变。", True),
        ("供体单元未受同一干预影响(数据无法检验)。", False),
    ),
}


def _f(value: Any) -> float | None:
    """宽容数值化(``None`` / 非数 → None;不抛)。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── L2 差中差 ─────────────────────────────────────────────
# ``did_2x2`` 的定义在文件顶端 re-export(本体:analysis/effect.py)。


def placebo_pairs(
    treated: Sequence[Any], control: Sequence[Any],
    blocks: Sequence[Sequence[str]] | None = None,
    *, count: int = DEFAULT_PLACEBO_BLOCKS,
) -> list[dict[str, Any]]:
    """前窗相邻块对的 DiD(placebo)—— 四值齐备才算一对,取最近 ``count`` 对。

    ``treated`` / ``control`` = **历史块**序列(不含当期;与 ``blocks``
    等长时逐对带窗口标签)。不够成对(缺值)的块**跳过而不是补零** ——
    补出来的对会稀释漂移散布,而散布正是唯一噪声来源。
    """
    pairs: list[dict[str, Any]] = []
    n = min(len(treated or []), len(control or []))
    if blocks is not None:
        n = min(n, len(blocks))
    for j in range(1, n):
        did = did_2x2(treated[j - 1], treated[j], control[j - 1], control[j])
        if did is None:
            continue
        pair: dict[str, Any] = {"did": did["att"],
                                "treated_delta": did["treated_delta"],
                                "control_delta": did["control_delta"]}
        if blocks is not None:
            pair["window"] = [str(blocks[j][0]), str(blocks[j][1])]
        pairs.append(pair)
    try:
        keep = max(int(count), 0)
    except (TypeError, ValueError):
        keep = DEFAULT_PLACEBO_BLOCKS
    return pairs[-keep:] if keep else pairs


def parallel_trends(
    pairs: Sequence[dict[str, Any]], *, scale: Any, tolerance: Any,
) -> dict[str, Any]:
    """前窗平行检验:``max|did| ≤ tolerance × scale``。

    ``scale`` = 前窗水平量级(调用方取 ``max(|T_pre|, |C_pre|)``);
    量级为 0/缺失 → ``zero_scale``(相对容差在零量级上无意义,不升级)。
    返回体带 ``measured``(max|did|)与 ``threshold``(tolerance×scale)——
    失败时审计要看得见「差多少才算过」。
    """
    tol = _f(tolerance)
    sc = _f(scale)
    body: dict[str, Any] = {
        "count": len(pairs or []),
        "tolerance": tol if tol is not None else DEFAULT_TOLERANCE,
        "pairs": [dict(p) for p in (pairs or [])],
        "max_abs_did": None, "scale": sc, "threshold": None,
        "passes": False, "reason": "",
    }
    if sc is None or not (sc > 0):
        body["reason"] = "zero_scale"
        return body
    tol_eff = tol if tol is not None and tol > 0 else DEFAULT_TOLERANCE
    threshold = tol_eff * sc
    body["threshold"] = threshold
    body["tolerance"] = tol_eff
    if not pairs:
        body["reason"] = "no_placebo_pairs"
        return body
    max_abs = max(abs(float(p["did"])) for p in pairs)
    body["max_abs_did"] = max_abs
    if max_abs <= threshold:
        return body | {"passes": True}
    body["reason"] = "placebo_not_parallel"
    return body


def did_se(pairs: Sequence[dict[str, Any]]) -> float | None:
    """placebo 漂移的标准差(≥2 对,``ddof=1``)—— **非模型标准误**。

    2×2 只有四个数、没有残差自由度;前窗漂移的散布是唯一不发明
    分布的噪声估计。少于 2 对 → ``None``(判不了,不返回 0)。
    """
    vals = [_f(p.get("did")) for p in (pairs or [])]
    nums = [v for v in vals if v is not None]
    if len(nums) < 2:
        return None
    m = fmean(nums)
    return (sum((v - m) ** 2 for v in nums) / (len(nums) - 1)) ** 0.5


def crosses_zero(effect: Any, se: Any) -> bool | None:
    """``|effect| ≤ 2·se`` → True(朴素带;se 缺失 → None = 判不了)。"""
    e, s = _f(effect), _f(se)
    if e is None or s is None:
        return None
    return abs(e) <= CROSSES_ZERO_BAND * s


# ── L3 合成对照(无优化器) ────────────────────────────────


def donor_counterfactual(
    treated: Sequence[Any], donors: dict[str, Sequence[Any]],
    *, tolerance: Any = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """等权供体平均的合成对照(供体 = 未受干预的同维单元)。

    序列约定(**与 ``BlockSeries`` 一致**):``序列[-1]`` = 当期,
    其余 = 历史;``donors[标签]`` 与 ``treated`` 等长(同一条分组查询
    的产物)。四步:

      1. 前窗 index 拟合:各块按前窗均值归一化,
         ``fit = max_j |T_j/T̄ − D_j/D̄|``(T̄/D̄ = 前窗均值,block 均值
         只用该块有值的供体)—— ``fit ≤ tolerance`` 才准升 L3;
      2. 反事实:``cf_post = T̄ × (D_post / D̄)``(供体当期的整体变动率
         外推到处理组);
      3. 效应:``effect = T_post − cf_post``;
      4. 数据不足的供体进 ``skipped`` 不参与(每个供体需前窗有值
         ∧ 当期有值)—— 一个供体都没有 → ``no_donors``(不升级)。

    这是刻意的最简可解释版:**没有优化器、没有权重求解** —— 权重
    合成需要数值优化,超出零依赖边界,不做。
    """
    tol = _f(tolerance)
    tol_eff = tol if tol is not None and tol > 0 else DEFAULT_TOLERANCE
    t = [_f(v) for v in (treated or [])]
    body: dict[str, Any] = {
        "donors": [], "skipped": [], "tolerance": tol_eff,
        "fit_relative": None, "fit_passes": False,
        "counterfactual": None, "effect": None, "reason": "",
    }
    if len(t) < 3:
        body["reason"] = "insufficient_blocks"
        return body
    t_hist, t_post = t[:-1], t[-1]
    t_vals = [v for v in t_hist if v is not None]
    if not t_vals:
        body["reason"] = "insufficient_blocks"
        return body
    t_bar = fmean(t_vals)
    if t_bar == 0:
        body["reason"] = "zero_scale"
        return body
    if t_post is None:
        body["reason"] = "no_post_value"
        return body

    used: list[str] = []
    cols: list[list[float | None]] = []
    posts: list[float] = []
    for label in sorted(donors or {}):
        d = [_f(v) for v in donors[label]]
        if len(d) != len(t) or d[-1] is None or all(v is None for v in d[:-1]):
            body["skipped"].append(str(label))
            continue
        used.append(str(label))
        cols.append(d[:-1])
        posts.append(d[-1])
    body["donors"] = used
    if not used:
        body["reason"] = "no_donors"
        return body

    idx_pairs: list[tuple[float, float]] = []
    for j in range(len(t_hist)):
        tv = t_hist[j]
        dv = [c[j] for c in cols if c[j] is not None]
        if tv is not None and dv:
            idx_pairs.append((tv, fmean(dv)))
    if len(idx_pairs) < 2:
        body["reason"] = "insufficient_blocks"
        return body
    d_bar = fmean(dv for _, dv in idx_pairs)
    if d_bar == 0:
        body["reason"] = "zero_scale"
        return body

    fit = max(abs(tv / t_bar - dv / d_bar) for tv, dv in idx_pairs)
    d_post = fmean(posts)
    cf_post = t_bar * (d_post / d_bar)
    body["fit_relative"] = fit
    body["counterfactual"] = cf_post
    body["effect"] = t_post - cf_post
    if fit <= tol_eff:
        body["fit_passes"] = True
    else:
        body["reason"] = "fit_too_poor"
    return body


# ── 梯子判定 ──────────────────────────────────────────────


def ladder_decision(
    *,
    seasonal_declared: bool,
    n_blocks: int,
    has_pre_block: bool,
    has_control: bool,
    budget_ok: bool,
    placebo: dict[str, Any] | None,
    mode: str = DEFAULT_CAUSAL_MODE,
    synthetic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """条件清单 → ``{rung, unmet}``(纯;所有条件在调用前已被实测)。

    条件(降级时 ``unmet`` 逐条给出实测值与阈值):

      - ``C1`` 稳定基线:``seasonal`` 已声明 ∧ 历史有值块数 ≥ ``MIN_BLOCKS``;
      - ``C2`` 对照可构造:对照组序列的 pre/post 齐备;
      - ``C3`` 前窗平行:placebo ``max|did| ≤ tolerance × scale``;
        ``placebo=None``(检验做不出)→ 不升级,reason ``placebo_unverifiable``;
      - ``C4`` 块对齐:块规格可规划、pre 块有值(结构性,先于 C1);
      - ``C5`` 预算:因果阶段的查询额度(让路时由调用方记账);
      - ``C6`` 供体拟合(仅 ``mode="auto"``):合成对照前窗 index 偏差
        ≤ tolerance —— 只挡 L3,不过则诚实停在 L2。

    ``rung`` = 达成的最高级(C1..C4 全过 → L2;C6 也过且 auto → L3)。
    """
    unmet: list[dict[str, Any]] = []

    def _stop(condition: str, **fields: Any) -> dict[str, Any]:
        unmet.append({"condition": condition, **fields})
        return {"rung": "L1", "unmet": unmet}

    # C4(结构性,依赖它的判定排在前面:块都建不出来就无从谈 n 与 pre)
    if not seasonal_declared:
        return _stop("C4", reason="no_seasonal")
    if not has_pre_block:
        return _stop("C4", reason="no_pre_block")
    # C1 稳定基线
    if int(n_blocks or 0) < MIN_BLOCKS:
        return _stop("C1", reason="insufficient_blocks",
                     measured=int(n_blocks or 0), threshold=MIN_BLOCKS)
    # C2 对照
    if not has_control:
        return _stop("C2", reason="control_unavailable")
    # C5 预算(在展开反事实计算之前的行为门,由调用方实测后传入)
    if not budget_ok:
        return _stop("C5", reason="query_budget_exceeded")
    # C3 前窗平行
    if placebo is None:
        return _stop("C3", reason="placebo_unverifiable")
    if not placebo.get("passes"):
        return _stop("C3", reason=str(placebo.get("reason") or "placebo_not_parallel"),
                     measured=placebo.get("max_abs_did"),
                     threshold=placebo.get("threshold"))
    rung = "L2"
    # C6 供体拟合(仅 auto;did 档刻意不尝试合成对照)
    if mode == "auto":
        if synthetic is None:
            unmet.append({"condition": "C6", "reason": "synthetic_unavailable"})
        elif synthetic.get("fit_passes"):
            rung = "L3"
        else:
            unmet.append({"condition": "C6",
                          "reason": str(synthetic.get("reason") or "fit_too_poor"),
                          "measured": synthetic.get("fit_relative"),
                          "threshold": synthetic.get("tolerance")})
    return {"rung": rung, "unmet": unmet}


def _fmt2(value: Any) -> str:
    v = _f(value)
    return "—" if v is None else f"{v:,.2f}"


def assumptions_for(
    rung: str, *, placebo: dict[str, Any] | None = None,
    synthetic: dict[str, Any] | None = None,
    unmet: Sequence[dict[str, Any]] | None = None,
    context: str = "",
) -> list[dict[str, Any]]:
    """给定达成级 → 假设清单(**永远非空**),逐条附 ``checked`` 与实测细节。

    ``checked=True`` = 该条在本级被检验且通过;``checked=None`` = 数据
    无法检验(写出来而不是省略)。L1 的细节 = 未升级原因(每条带
    实测/阈值),审计一眼看到「卡在哪、差多少」。
    """
    rung = rung if rung in LADDER_ASSUMPTIONS else "L1"
    out: list[dict[str, Any]] = []
    if rung == "L1":
        why = "; ".join(
            f"{u.get('condition')}: {u.get('reason')}"
            + (f"(实测 {_fmt2(u.get('measured'))} / 阈值 {_fmt2(u.get('threshold'))})"
               if u.get("measured") is not None and u.get("threshold") is not None
               else "")
            for u in (unmet or [])) or "未声明升级条件"
        for text, _ in LADDER_ASSUMPTIONS["L1"]:
            out.append({"text": text, "checked": None, "detail": f"未升级:{why}"})
        return out
    pl = placebo or {}
    sync_detail = (
        f"max|did|={_fmt2(pl.get('max_abs_did'))} ≤ "
        f"{_fmt2(pl.get('threshold'))}({int(pl.get('count') or 0)} 对前窗)"
    )
    for text, checkable in LADDER_ASSUMPTIONS[rung]:
        detail = ""
        checked: bool | None = True if checkable else None
        if "平行" in text:
            detail = sync_detail
        elif "同步" in text and synthetic is not None:
            detail = (f"拟合偏差 {_fmt2(synthetic.get('fit_relative'))} ≤ "
                      f"{_fmt2(synthetic.get('tolerance'))}"
                      f"({len(synthetic.get('donors') or [])} 供体)")
        elif "无法检验" in text:
            detail = "无数据可检验,列出以显式化"
        else:
            detail = context
        out.append({"text": text, "checked": checked, "detail": detail})
    return out


def causal_line(section: dict[str, Any] | None) -> str:
    """因果证据节 → 一行消息缀(**仅 L2/L3 且有效应**;L1 不缀 —— 拒绝主张的效应不上通知)。"""
    section = section if isinstance(section, dict) else {}
    rung = str(section.get("rung") or "")
    if rung == "L3":
        body = section.get("synthetic") or {}
        att, design = body.get("effect"), "合成对照"
    elif rung == "L2":
        body = section.get("did") or {}
        att, design = body.get("att"), "DiD"
    else:
        return ""
    val = _f(att)
    if val is None:
        return ""
    cz = body.get("crosses_zero")
    tail = "区间跨零" if cz is True else ("区间不跨零" if cz is False else "噪声估计不足")
    return f"净效应：{val:+,.2f}（{design}·{tail}）"
