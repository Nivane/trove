"""判定质量回评 —— 按 ``(rule_id, rule_rev)`` 分桶的纯派生报告(零 I/O)。

闭环验收(B7)的另一半:行动出得去、效果测得回来,还要回答「**判得准
不准**」。三条纪律:

  1. **分桶键 =(rule_id, rule_rev)** —— N2 修复的延续。``rule_digest``
     是整份 decisions.yml 的字节 sha256,编辑 B 规则会让 A 规则的历史
     整段错位;``rule_rev`` 是单条规则的内容版本,**改 A 才开 A 的新桶**。
     更老的 verdict 没有 rev → 落 ``rev_unknown`` 桶(不猜、不丢:
     没有版本信息的那些判定仍然是一段可以被看的历史);
  2. **纯派生,绝不写回**。质量是读出来的视图,不是一张会漂移的表 ——
     判定记录不可变(见 verdicts.py 的第一条性质),把质量算好存回去
     的设计会让同一段历史在不同时刻给出不同的数;
  3. **比率只在分母够时给**。``effective_rate`` 的分母是**判得出来**的
     测量(有效 + 无变化),``unverifiable``(判不了)不进分母;
     ``insufficient`` 明列原因 —— 3 次行动里的 2 次有效不是「67% 有效率」,
     它是「样本不足,别读比率」。

判定健康(status 计数)与行动效果(effects)分开计:一条全是 error 的
规则(判定"判不了")与一条判了但从没动到数的规则,是两种完全不同的问题,
把它们的比率混在一起会把两者都盖住。
"""

from __future__ import annotations

from typing import Any, Iterable

from trove.services.analysis.stats import LOW_N

#: 无版本 verdict 的分桶 rev(空/缺失一律落这里,不猜成当前版本)。
REV_UNKNOWN = "rev_unknown"

#: 效果比率的最小分母 —— 低于它 ``effective_rate`` 给 None(样本不足)。
#: 与 ``LOW_N`` 是**两个域**:这里的是「多少次测量才敢说有效率」,
#: 取得比判定的 LOW_N 更小是因为测量本身稀缺(每个行动一条),
#: 但再小也要有个线:1/1 不是 100%。
MIN_EFFECTS = 3


def bucket_key(rule_id: str, rule_rev: str = "") -> str:
    """``"<rule_id>@<rule_rev|rev_unknown>"`` —— 分桶键的唯一构造处。"""
    rev = str(rule_rev or "").strip() or REV_UNKNOWN
    return f"{str(rule_id or '')}@{rev}"


def _get(obj: Any, key: str, default: Any = "") -> Any:
    """dict / 对象两读(verdict 记录与调用方拼的 dict 都是合法输入)。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def rev_of(verdict: Any) -> str:
    """verdict → 它的 rule_rev(在 ``evidence`` 里;B2 之前没有 → 空串)。

    ``evidence`` 是 dict(存储形状);为让测试能直接喂轻量对象,属性形
    也认。读不到 → 空串(由 :func:`bucket_key` 折成 ``rev_unknown``)。
    """
    evidence = _get(verdict, "evidence", None)
    if isinstance(evidence, dict):
        return str(evidence.get("rule_rev") or "")
    return str(getattr(verdict, "rule_rev", "") or "")


def _effect_flags(entry: dict[str, Any]) -> str:
    """效果条目 → ``"error" | "effective" | "no_effect" | "unverifiable"``。

    错误优先(测量失败是一档事实);``outside_band`` 用 ``is True`` /
    ``is False`` 判 —— 存储层的 0/1/None 与 Python 的 False/True/None
    都要落位,而 ``0`` 绝不是「没测到」。
    """
    if str(entry.get("error") or ""):
        return "error"
    band = entry.get("outside_band")
    if band is True or band == 1:
        return "effective"
    if band is False or band == 0:
        return "no_effect"
    return "unverifiable"


def _new_bucket(rule_id: str, rule_rev: str) -> dict[str, Any]:
    return {
        "key": bucket_key(rule_id, rule_rev),
        "rule_id": rule_id,
        "rule_rev": rule_rev or REV_UNKNOWN,
        "total": 0, "ok": 0, "alert": 0, "error": 0,
        "triggered": 0,
        "first_at": "", "last_at": "",
        "effects": {"measured": 0, "effective": 0, "no_effect": 0,
                    "unverifiable": 0, "errors": 0},
        "decided": 0,
        "triggered_rate": None,
        "effective_rate": None,
        "insufficient": [],
    }


def _touch(bucket: dict[str, Any], at: Any) -> None:
    stamp = str(at or "")
    if not stamp:
        return
    if not bucket["first_at"] or stamp < bucket["first_at"]:
        bucket["first_at"] = stamp
    if stamp > bucket["last_at"]:
        bucket["last_at"] = stamp


def score_history(
    verdicts: Iterable[Any] | None,
    *,
    effects: Iterable[dict[str, Any]] | None = None,
    min_effects: int = MIN_EFFECTS,
) -> list[dict[str, Any]]:
    """判定史(可选:效果条目)→ 分桶质量报告(纯;按 key 升序,确定性)。

    ``verdicts`` 每条读 ``rule_id`` / ``status`` / ``triggered`` /
    ``evaluated_at`` / ``evidence.rule_rev``;``effects`` 每条读
    ``rule_id`` / ``rule_rev`` / ``outside_band`` / ``error``(调用方
    负责把 outcome 与提案 join 好——那是 I/O 的活,不是这里的)。

    有测量但对应 verdict 已被保留期清掉的桶**照常出现**:效果是真的,
    历史被清了是另一件事(它由 ``total == 0`` 自己说出来)。
    """
    buckets: dict[str, dict[str, Any]] = {}

    def bucket_of(rule_id: str, rule_rev: str) -> dict[str, Any]:
        key = bucket_key(rule_id, rule_rev)
        if key not in buckets:
            buckets[key] = _new_bucket(rule_id, rule_rev)
        return buckets[key]

    for v in verdicts or []:
        rule_id = str(_get(v, "rule_id", "") or "")
        bucket = bucket_of(rule_id, rev_of(v))
        bucket["total"] += 1
        status = str(_get(v, "status", "") or "")
        if status in ("ok", "alert", "error"):
            bucket[status] += 1
        if _get(v, "triggered", False):
            bucket["triggered"] += 1
        _touch(bucket, _get(v, "evaluated_at", ""))

    for e in effects or []:
        if not isinstance(e, dict):
            continue
        bucket = bucket_of(str(e.get("rule_id") or ""),
                           str(e.get("rule_rev") or ""))
        flag = _effect_flags(e)
        if flag == "error":
            bucket["effects"]["errors"] += 1
        else:
            bucket["effects"]["measured"] += 1
            bucket["effects"][flag] += 1
        _touch(bucket, e.get("anchor_date") or e.get("measured_at") or "")

    out: list[dict[str, Any]] = []
    for key in sorted(buckets):
        bucket = buckets[key]
        total = bucket["total"]
        bucket["triggered_rate"] = (bucket["triggered"] / total) if total else None
        decided = bucket["effects"]["effective"] + bucket["effects"]["no_effect"]
        bucket["decided"] = decided
        if decided >= int(min_effects):
            bucket["effective_rate"] = bucket["effects"]["effective"] / decided
        insufficient: list[str] = []
        if total and total < LOW_N:
            insufficient.append("few_verdicts")
        if 0 < decided < int(min_effects):
            insufficient.append("few_effects")
        if decided == 0:
            insufficient.append("no_effects")
        bucket["insufficient"] = insufficient
        out.append(bucket)
    return out


def rollup(
    buckets: Iterable[dict[str, Any]] | None,
    *,
    min_effects: int = MIN_EFFECTS,
) -> dict[str, Any]:
    """各桶 → 总览(同一套口径:计数相加,比率重算,不足原因重判)。

    ``effective_rate`` 与桶同一道门(``decided >= min_effects``,不足 →
    None + ``insufficient``):总览行不该比桶更敢说话 —— 一条测量的
    100% 正是本模块要防的伪精度(3 次行动里的 2 次不是「67%」,
    1 次里的 1 次更不是「100%」)。
    """
    items = list(buckets or [])
    total = ok = alert = error = triggered = 0
    effects = {"measured": 0, "effective": 0, "no_effect": 0,
               "unverifiable": 0, "errors": 0}
    for b in items:
        total += int(b.get("total") or 0)
        ok += int(b.get("ok") or 0)
        alert += int(b.get("alert") or 0)
        error += int(b.get("error") or 0)
        triggered += int(b.get("triggered") or 0)
        for k in effects:
            effects[k] += int((b.get("effects") or {}).get(k) or 0)
    decided = effects["effective"] + effects["no_effect"]
    insufficient: list[str] = []
    if 0 < decided < int(min_effects):
        insufficient.append("few_effects")
    if decided == 0:
        insufficient.append("no_effects")
    return {
        "buckets": len(items),
        "total": total, "ok": ok, "alert": alert, "error": error,
        "triggered": triggered,
        "triggered_rate": (triggered / total) if total else None,
        "effects": effects,
        "decided": decided,
        "effective_rate": ((effects["effective"] / decided)
                           if decided >= int(min_effects) else None),
        "insufficient": insufficient,
    }
