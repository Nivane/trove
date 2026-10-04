"""what-if 模拟 —— 判定内核在假想数字上的重判(**纯,零查询**)。

三条纪律:

  - **零查询**:数据来源只有两条 —— 重放最近一条 verdict 的
    ``evidence.rows``(判定当时到底在哪些数字上判的),或调用方直接
    给的 maps。绝不为了「如果…会怎样」去查业务库:模拟的价值在于
    **同一条内核**(``service.judge``)在同一份数字上重跑,而不是再取
    一份数字——再取一份,判的就不是同一个东西了;
  - **没模拟到的要写出来**:场景点名了未知维度/缺值算不动 → 进
    ``unapplied``(带原因),不静默丢弃 —— 少模拟了两项的结论与完整
    结论在输出上必须可分辨;
  - **不装能算**:驱动器树里非加性节点(比率/乘法)的子项变化量之和
    ≠ 父变化量,该节点标 ``not_modeled`` 并把 ``None`` 传给父级 ——
    宁可不给数,不造恒等式(与 ``expr_tree`` 的 decomposable 纪律
    同源)。

判定与模拟共用 ``judge`` 是两者不漂移的全部保证;噪声带位置
(``confidence``/``gated``)只重放不重算 —— 假想数字没有历史带可站,
响应以 ``degraded`` 明说「带没有参与这次模拟」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trove.services.decision.rules import DecisionRule, compile_condition
from trove.services.decision.service import judge

#: 场景可调整的两侧。``current`` = 当期值(最常问的「如果它再降 10%」),
#: ``baseline`` = 基期值(问「如果去年本来就低」)。
FIELDS = ("current", "baseline")
#: ``pct`` = 相对变化(``-0.1`` 是降 10%),``abs`` = 绝对增减,``set`` = 直接置值。
MODES = ("pct", "abs", "set")
#: 一条调整的闭键集(与 rules 的闭键集同款:未知键报错并列出合法键)。
ADJUSTMENT_KEYS = ("dim", "field", "mode", "value")


class WhatIfError(ValueError):
    """Malformed scenario — a write-surface error, never a quiet no-op."""


@dataclass
class Adjustment:
    """一条假想调整:对 ``dim`` 组的 ``field`` 侧做 ``mode`` 调整。"""

    dim: str = ""          # "" = 聚合规则唯一的组
    field: str = "current"
    mode: str = "pct"
    value: float = 0.0


def parse_scenario(raw: Any) -> list[Adjustment]:
    """场景 JSON → ``Adjustment`` 列表;结构错误抛 ``WhatIfError``。

    空/缺省 = 空场景(合法:只重放判定,不改变任何数字)。未知键、
    未知 field/mode、非数值 value 全部在解析期拒绝 —— 静默忽略一条
    调整会让「模拟过了」与「模拟的是别的场景」看起来一样。
    """
    if raw is None or raw == "":
        return []
    if not isinstance(raw, list):
        raise WhatIfError(
            f"'scenario' must be a list of adjustments, got {type(raw).__name__}")
    out: list[Adjustment] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise WhatIfError(f"scenario[{i}] must be a mapping, "
                              f"got {type(item).__name__}")
        unknown = [k for k in item if k not in ADJUSTMENT_KEYS]
        if unknown:
            raise WhatIfError(
                f"scenario[{i}] has unknown keys {unknown} — legal keys: "
                f"{', '.join(ADJUSTMENT_KEYS)}")
        field = str(item.get("field") or "current").strip().lower()
        if field not in FIELDS:
            raise WhatIfError(f"scenario[{i}].field must be one of "
                              f"{', '.join(FIELDS)} (got {field!r})")
        mode = str(item.get("mode") or "pct").strip().lower()
        if mode not in MODES:
            raise WhatIfError(f"scenario[{i}].mode must be one of "
                              f"{', '.join(MODES)} (got {mode!r})")
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WhatIfError(f"scenario[{i}].value must be a number "
                              f"(got {value!r})")
        out.append(Adjustment(dim=str(item.get("dim") or "").strip(),
                              field=field, mode=mode, value=float(value)))
    return out


def apply_adjustments(
    adjustments: list[Adjustment],
    cur_map: dict[str, float | None],
    base_map: dict[str, float | None],
) -> tuple[dict[str, float | None], dict[str, float | None],
           list[dict[str, Any]], list[dict[str, Any]]]:
    """(cur, base) → (假想 cur, 假想 base, applied, unapplied)。

    ``unapplied`` 的两种原因都是**响亮**的:``unknown_dim``(点名了
    重放数据里没有的组)/ ``missing_value``(该组这一侧本来就没值,
    pct/abs 算不动 —— ``set`` 例外,置值不需要旧值)。原 maps 不被
    改动(调用方可能还要拿它做 before 判定)。
    """
    new_cur = dict(cur_map)
    new_base = dict(base_map)
    applied: list[dict[str, Any]] = []
    unapplied: list[dict[str, Any]] = []
    for a in adjustments:
        target = new_cur if a.field == "current" else new_base
        if a.dim not in target:
            unapplied.append({"dim": a.dim, "field": a.field, "mode": a.mode,
                              "value": a.value, "reason": "unknown_dim"})
            continue
        before = target.get(a.dim)
        if before is None and a.mode != "set":
            unapplied.append({"dim": a.dim, "field": a.field, "mode": a.mode,
                              "value": a.value, "reason": "missing_value"})
            continue
        if a.mode == "pct":
            after = before * (1 + a.value)
        elif a.mode == "abs":
            after = before + a.value
        else:
            after = a.value
        target[a.dim] = after
        applied.append({"dim": a.dim, "field": a.field, "mode": a.mode,
                        "value": a.value, "before": before, "after": after})
    return new_cur, new_base, applied, unapplied


def _judged(
    rule: DecisionRule, cond: Any, cur_map: dict[str, float | None],
    base_map: dict[str, float | None], row_count: int, *,
    confidence_by_dim: dict[str, float | None] | None,
    gated_by_dim: dict[str, bool] | None,
    require_gate: bool,
) -> dict[str, Any]:
    rows, dims = judge(rule, cond, cur_map, base_map, row_count,
                       confidence_by_dim=confidence_by_dim,
                       gated_by_dim=gated_by_dim, require_gate=require_gate)
    return {"triggered": bool(dims), "rows": rows, "emitted": list(dims)}


def simulate_rule(
    rule: DecisionRule,
    cur_map: dict[str, float | None],
    base_map: dict[str, float | None],
    row_count: int,
    adjustments: list[Adjustment] | None = None, *,
    confidence_by_dim: dict[str, float | None] | None = None,
    gated_by_dim: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """同一条 ``judge`` 内核跑两遍:原数字 = 判定,假想数字 = 模拟。

    ``confidence_by_dim``/``gated_by_dim`` 只在**重放**时给定(verdict
    行卡上带着当时的带位)—— 它们原样参与两次判定,**不重算**;声明了
    ``significance`` 的规则在 ``degraded`` 里明说这一点,免得「模拟里
    触发」被读成「带也确认了」。

    没有带位数据时(调用方模式,或老 verdict 没记)``require_gate`` 不
    施加 —— 假装带没确认会把所有组判成「不触发」,读起来像「这条规则
    永远不响」,比不给模拟更糟。此时翻转是**条件级**的,``degraded``
    里的 ``not_simulated`` 立刻说明。
    """
    cond = compile_condition(rule)
    gate_available = confidence_by_dim is not None
    require_gate = bool(gate_available and rule.significance is not None
                        and rule.significance.required())
    common = dict(confidence_by_dim=confidence_by_dim,
                  gated_by_dim=gated_by_dim, require_gate=require_gate)
    before = _judged(rule, cond, cur_map, base_map, row_count, **common)
    hyp_cur, hyp_base, applied, unapplied = apply_adjustments(
        list(adjustments or []), cur_map, base_map)
    after = _judged(rule, cond, hyp_cur, hyp_base, row_count, **common)
    flip = "none"
    if after["triggered"] and not before["triggered"]:
        flip = "fired"
    elif before["triggered"] and not after["triggered"]:
        flip = "cleared"
    degraded: list[dict[str, Any]] = []
    if rule.significance is not None:
        degraded.append({
            "stage": "significance",
            "reason": "not_simulated" if confidence_by_dim is None
                      else "replayed_not_recomputed",
        })
    return {
        "applied": applied,
        "unapplied": unapplied,
        "before": before,
        "after": after,
        "flip": flip,
        "degraded": degraded,
    }


def simulate_tree(root: dict[str, Any], impacts: dict[str, float]) -> dict[str, Any]:
    """驱动器树 + {组件: 变化量} → 根级总变化(**只在加性链上**)。

    逐节点自底向上:叶子取 ``impacts``(键按 ``candidate`` 或 ``name``
    匹配,大小写不敏感;没给 = 假设不变,计 ``assumed_unchanged``);
    ``decomposable`` 节点 = 子项和(``-`` 取差);**非加性节点(比率/
    乘法/均值)不装能算** —— 标 ``not_modeled`` 并让父级一起不可算
    (子项之一不可算,父级就没有可信的总量)。``total is None`` 是
    合法且最有信息量的结果:这棵树的结构不支持把组件变化加总到根。
    """
    lowered = {str(k).strip().lower(): float(v) for k, v in (impacts or {}).items()}
    not_modeled: list[dict[str, Any]] = []
    assumed = 0

    def walk(node: dict[str, Any]) -> float | None:
        nonlocal assumed
        kids = node.get("children") or []
        name = str(node.get("name") or node.get("candidate") or "")
        if not kids:
            cand = str(node.get("candidate") or "").strip().lower()
            key = str(node.get("name") or "").strip().lower()
            if cand and cand in lowered:
                return lowered[cand]
            if key in lowered:
                return lowered[key]
            assumed += 1
            return 0.0
        vals = [walk(k) for k in kids]
        if not node.get("decomposable"):
            not_modeled.append({"node": name, "op": node.get("op"),
                                "reason": "non_additive",
                                "detail": "子项变化量之和不等于父变化量"})
            return None
        if any(v is None for v in vals):
            not_modeled.append({"node": name, "op": node.get("op"),
                                "reason": "child_not_modeled"})
            return None
        if node.get("op") == "-" and len(vals) == 2:
            return vals[0] - vals[1]
        return sum(vals)

    total = walk(root)
    return {"total": total, "not_modeled": not_modeled,
            "assumed_unchanged": assumed}


def impact_summary(sim: dict[str, Any], *,
                   tree: dict[str, Any] | None = None) -> dict[str, Any]:
    """模拟结果 → 一句话 + 机器可读字段(确定性,零 LLM)。

    判据:`flip`(触发翻转)、逐条 ``applied`` 的前后值、``unapplied``
    的条数(没模拟到的必须计数),以及(给了树时)根级总变化 —— 树
    算不出时 ``total_delta=None`` 与「总变化是 0」在字段上就分得开。
    """
    applied = sim.get("applied") or []
    unapplied = sim.get("unapplied") or []
    before, after = sim.get("before") or {}, sim.get("after") or {}
    parts = []
    for a in applied:
        label = f"{a['dim']} " if a["dim"] else ""
        if a["mode"] == "pct":
            how = f"{a['value'] * 100:+.1f}%"
        elif a["mode"] == "abs":
            how = f"{a['value']:+,.2f}"
        else:
            how = f"→ {a['value']:,.2f}"
        parts.append(f"{label}{a['field']} {how}"
                     f"({_num(a['before'])}→{_num(a['after'])})")
    head = "; ".join(parts) or "空场景(仅重放)"
    line = (f"what-if: {head} — 触发 {'是' if before.get('triggered') else '否'}"
            f"→{'是' if after.get('triggered') else '否'}"
            f"(翻转:{sim.get('flip')})")
    if unapplied:
        line += f";{len(unapplied)} 条调整未模拟"
    if tree is not None:
        total = tree.get("total")
        line += (";根级总变化 " + _num(total)) if total is not None \
            else ";根级总变化 不可算"
    out: dict[str, Any] = {
        "flip": sim.get("flip"),
        "triggered_before": bool(before.get("triggered")),
        "triggered_after": bool(after.get("triggered")),
        "changed": [f"{a['dim']}.{a['field']}" if a["dim"] else a["field"]
                    for a in applied],
        "unapplied_count": len(unapplied),
        "total_delta": (tree or {}).get("total"),
        "line": line,
    }
    return out


def _num(v: Any) -> str:
    """与判定消息同一数字格式(``service._fmt`` 的口径)。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "—"
    return f"{float(v):,.2f}".rstrip("0").rstrip(".")
