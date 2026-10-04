"""确定性分解数学 —— 纯函数,零 LLM,零 I/O。

从 ``workflow/nodes/attribution.py`` 原样升格而来(行为逐字不变,
``tests/workflow/test_attribution.py`` 是搬迁的安全网)。三个调用方:
图内 attribution 节点、前端分析卡、以及未来的 agentic 分析循环与
决策 what-if(推测性评估复用同一套数学)。

全部函数无时间依赖、无副作用:给定输入必然给定输出,可直接单测。
"""

from __future__ import annotations

from typing import Any

# 浮点序误差容忍:分解恒等式判定(残差是否"为零")用绝对容差。
# 加性链上 delta 精确相加,误差只来自浮点表示,1e-9 足够宽。
_EXACT_TOL = 1e-9


def num(value: Any) -> float:
    """容忍数值解析:None/空 → 0.0;字符串去 %/逗号/货币符号。"""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    s = s.replace(",", "").replace("，", "").lstrip("¥$€£￥")
    try:
        return float(s)
    except ValueError:
        return 0.0


def contribution(
    base_map: dict[str, float],
    cur_map: dict[str, float],
) -> list[dict[str, Any]]:
    """维度贡献率表(确定性)。

    - delta_i = cur_i - base_i(缺失维度按 0 计);
    - total_abs = Σ|delta_i|;contribution_i = delta_i / total_abs(带符号);
    - total_abs == 0(无变化)→ 退化为占比归因 cur_i / Σcur_i;
    - 结果按 |contribution| 降序(top 贡献者在前,下钻用)。

    Returns: [{"dim", "base", "current", "delta", "contribution"}, ...]
    """
    # 保序并集(dict.fromkeys):集合迭代序随 PYTHONHASHSEED 变化,而
    # 并列项的排序会退到插入序 —— 跨进程必须逐字节一致。
    keys = dict.fromkeys(list(base_map) + list(cur_map))
    items: list[dict[str, Any]] = []
    for k in keys:
        b = num(base_map.get(k, 0.0))
        c = num(cur_map.get(k, 0.0))
        items.append({"dim": k, "base": b, "current": c, "delta": c - b})
    total_abs = sum(abs(it["delta"]) for it in items)
    if total_abs == 0:
        total_cur = sum(it["current"] for it in items) or 1.0
        for it in items:
            it["contribution"] = it["current"] / total_cur
    else:
        for it in items:
            it["contribution"] = it["delta"] / total_abs
    # 二级键:并列时按维度名升序 —— 单键排序的并列次序依赖插入序,
    # 跨进程不可复现
    items.sort(key=lambda x: (-abs(x["contribution"]), str(x["dim"])))
    return items


def shift_share(base_nd: dict[str, tuple[float, float]], cur_nd: dict[str, tuple[float, float]]) -> dict[str, Any]:
    """比率指标变化的 shift-share 分解(精确恒等式)。

    R = Σ_i rate_i * weight_i,其中 rate_i = n_i/d_i,weight_i = d_i/Σd。
    ΔR = R_cur − R_base 拆成三部分,每项按组可加、总和精确等于 ΔR:
      - within(本征/率效应):Σ w_base_i * (rate_cur_i − rate_base_i)
      - composition(结构/混合效应):Σ (w_cur_i − w_base_i) * rate_base_i
      - interaction(交叉项):Σ (w_cur_i − w_base_i) * (rate_cur_i − rate_base_i)
    每组的 contribution_i = w_cur*r_cur − w_base*r_base(= 三效应之和)。

    Returns: {rows, effects, base_total, cur_total}。rows 按 |contribution|
    降序,含 base_rate/current_rate/base_weight/current_weight/within/
    composition/interaction/contribution;effects 为三类效应总和 + ΔR。
    """
    dims = dict.fromkeys(list(base_nd) + list(cur_nd))  # 保序并集,跨进程确定
    D_base = sum(d for _, d in base_nd.values())
    D_cur = sum(d for _, d in cur_nd.values())
    N_base = sum(n for n, _ in base_nd.values())
    N_cur = sum(n for n, _ in cur_nd.values())
    R_base = N_base / D_base if D_base else 0.0
    R_cur = N_cur / D_cur if D_cur else 0.0
    rows: list[dict[str, Any]] = []
    for k in dims:
        n_b, d_b = base_nd.get(k, (0.0, 0.0))
        n_c, d_c = cur_nd.get(k, (0.0, 0.0))
        r_b = n_b / d_b if d_b else 0.0
        r_c = n_c / d_c if d_c else 0.0
        w_b = d_b / D_base if D_base else 0.0
        w_c = d_c / D_cur if D_cur else 0.0
        within = w_b * (r_c - r_b)
        composition = (w_c - w_b) * r_b
        interaction = (w_c - w_b) * (r_c - r_b)
        contribution = within + composition + interaction
        rows.append({
            "dim": k,
            "base": r_b, "current": r_c, "delta": r_c - r_b,
            "base_rate": r_b, "current_rate": r_c,
            "base_weight": w_b, "current_weight": w_c,
            "within": within, "composition": composition, "interaction": interaction,
            "contribution": contribution,
        })
    rows.sort(key=lambda r: (-abs(r["contribution"]), str(r["dim"])))
    effects = {
        "within": sum(r["within"] for r in rows),
        "composition": sum(r["composition"] for r in rows),
        "interaction": sum(r["interaction"] for r in rows),
        "delta": R_cur - R_base,
        "base_rate": R_base, "current_rate": R_cur,
    }
    return {"rows": rows, "effects": effects, "base_total": R_base, "cur_total": R_cur}


def ratio_share(cur_nd: dict[str, tuple[float, float]]) -> dict[str, Any]:
    """比率指标占比归因(share 基线,无基期):贡献 = 分子份额 n_i/N。"""
    N = sum(n for n, _ in cur_nd.values())
    D = sum(d for _, d in cur_nd.values())
    R = N / D if D else 0.0
    rows: list[dict[str, Any]] = []
    for k, (n, d) in cur_nd.items():
        rate = n / d if d else 0.0
        weight = d / D if D else 0.0
        rows.append({
            "dim": k,
            "base": rate, "current": rate, "delta": rate,
            "base_rate": rate, "current_rate": rate,
            "base_weight": weight, "current_weight": weight,
            "within": 0.0, "composition": 0.0, "interaction": 0.0,
            "contribution": (n / N) if N else 0.0,
        })
    rows.sort(key=lambda r: (-abs(r["contribution"]), str(r["dim"])))
    return {"rows": rows, "effects": None, "base_total": 0.0, "cur_total": R}


def breakdown_signal(cur_map: dict[str, Any], base_map: dict[str, Any], ratio_parts: tuple[str, str] | None) -> float:
    """维度解释力信号:Σ|per-group Δ|(加性=值变化,比率=分解贡献)。

    加性:Σ|cur_i − base_i|(与 ``contribution`` 的 total_abs 一致);比率:
    shift-share 分解后 Σ|contribution_i|(各组对整体率变化的贡献绝对值)。
    """
    if not cur_map and not base_map:
        return 0.0
    if ratio_parts:
        dec = shift_share(
            {k: tuple(map(float, v)) for k, v in (base_map or {}).items()},
            {k: tuple(map(float, v)) for k, v in (cur_map or {}).items()},
        )
        return sum(abs(r["contribution"]) for r in dec["rows"])
    # 保序并集:浮点求和的次序影响末位比特,跨进程也要一致
    keys = dict.fromkeys(list(cur_map) + list(base_map))
    return sum(abs(float(cur_map.get(k, 0.0)) - float(base_map.get(k, 0.0))) for k in keys)


def signed_children(op: str | None, deltas: list[float]) -> list[float]:
    """按算子把子变化量变成"对父变化的贡献":减法第二项取负。

    Δ(a − b) = Δa − Δb —— 直接对子 delta 求和会把减法链误判出残差。
    其余算子(加/乘/除)原样返回;乘除链本就不声称分解(decomposable
    =False),这里的输出只用于加性恒等式判定。
    """
    if op == "-" and len(deltas) == 2:
        return [deltas[0], -deltas[1]]
    return list(deltas)


def residual(parent_delta: float, child_deltas: list[float]) -> dict[str, Any]:
    """残差:父变化量 − 子组件变化量之和(分解诚实性判定)。

    ``exact=True`` 当且仅当残差在浮点容差内为零 —— 加性链上子项之和
    精确等于父项(恒等式);乘除链上不为零,此时**不得**声称精确分解,
    残差如实入 payload(宁可不拆,不造恒等式)。
    """
    gap = float(parent_delta) - sum(float(d) for d in child_deltas)
    return {"value": gap, "exact": abs(gap) <= _EXACT_TOL}
