"""体检结局的闭集、分类优先级、指标与 diff 方向 —— 纯函数,零 I/O,零 LLM。

**这是唯一权威**:engine 只负责把一次节点回放后的状态交给 :func:`classify`;
report、API 与前端都只消费这里产出的形状。口径写在这里而不是散在
engine/service 里,是因为「哪些结局进分母」一旦有两个说法,两个数字就会
各自漂移 —— 而体检的全部价值就是那两个数字可信。

**十个结局,没有第十一种**:

============  ==========================================================  ========
outcome       定义                                                        进分母
============  ==========================================================  ========
certified     快径模板命中(``fast_path`` + SQL,零 LLM)                    是
compiled      ``compile_meta.outcome == "compiled"``(完整编译)            是
partial       ``outcome == "partial"``(骨架存在,有未覆盖组件)              是
soft_miss     软 MISS 且无拒绝(直通生成的未受保护缺口)                      是
refused       硬 MISS + 拒绝信号(语义模型的结构性缺口)                      是
unmatched     无语义模型 / 语义锚定零命中 / ``no_semantic_layer``            是
not_query     确定性意图为 metadata/write/chitchat/correction/confirm      否
unplanned     计划校验丢弃 / 空计划 / 无计划意图                            否
error         超时 / LLM 异常 / 节点吞异常 / 重规划不收敛                    否
skipped       取消 / 运行超时 / 运行级失败而未处理                           否
============  ==========================================================  ========

两个覆盖率的分母都只含 **evaluated**(前六个结局):

- 可确定性覆盖率 = (certified + compiled) / evaluated —— 不依赖生成兜底的覆盖;
- 受治理覆盖率   = (+ partial) / evaluated —— 骨架也计入(有未覆盖组件但可
  按骨架交付,受执行前骨架保真校验保护)。

``soft_miss`` **不并进拒绝**:它仍可能由生成通道回答,把它算成缺口会虚低;
``unplanned``/``error`` 也不并进分母 —— 它们不是「模型覆盖不了」,而是
「这一问没判出来」。两类都单列,绝不洗成 0。
"""

from __future__ import annotations

from typing import Any

#: 结局闭集(顺序 = 报表/前端展示顺序:先覆盖,后缺口,再不可判)。
OUTCOMES: tuple[str, ...] = (
    "certified",
    "compiled",
    "partial",
    "soft_miss",
    "refused",
    "unmatched",
    "not_query",
    "unplanned",
    "error",
    "skipped",
)

#: 计入覆盖率分母的结局。
EVALUATED_OUTCOMES: frozenset[str] = frozenset({
    "certified", "compiled", "partial", "soft_miss", "refused", "unmatched",
})

#: 未判出/非查询类(report 里单列,不进口径)。
UNMEASURED_OUTCOMES: frozenset[str] = frozenset({
    "not_query", "unplanned", "error", "skipped",
})

#: diff 方向判定用的覆盖序(越大越好;其余结局不可比)。
COVERAGE_RANK: dict[str, int] = {
    "unmatched": 1,
    "refused": 2,
    "soft_miss": 3,
    "partial": 4,
    "compiled": 5,
    "certified": 6,
}

#: 体检口径的版本 —— 结局闭集或分母定义变化时 +1(历史 run 带着它落库,
#: 跨版本的趋势对比必须能说清「这两个数不是同一把尺」)。
TAXONOMY_VERSION = 1

#: ``high_unmeasured`` 告警阈值:未判出(unplanned+error)超过总题数的比例。
HIGH_UNMEASURED_RATIO = 0.20


def classify(
    *,
    intent: str,
    fast_path: bool,
    no_model: bool,
    refusal_reason: str,
    compile_meta: dict[str, Any] | None,
    plan_validation_status: str,
    last_call_error: bool,
    llm_calls: int,
) -> tuple[str, str]:
    """一次回放后的状态 → ``(outcome, outcome_reason)``。

    优先级是**精确的**(判定顺序就是语义;测试逐分支钉住):

    1. 非查询意图 → ``not_query``(零 LLM,不进入后续节点);
    2. 快径命中 → ``certified``(即使 compile_meta 缺席);
    3. 无语义锚点 → ``unmatched``(no_model / no_semantic_match);
    4. 无 ``compile_meta`` 的三条合法路径:计划校验丢弃 / LLM 最终调用失败 /
       零调用(节点在 LLM 之前就异常)→ 各自单列,绝不混成一个 ``error``;
    5. ``no_semantic_layer`` 短路分因 → ``unmatched``(与 no_model 同一件事);
    6. compiled / partial / soft_miss / refused 依次判定;
    7. 剩下的 miss 无拒绝信号 = 计划没表达可编译意图 → ``unplanned``。
    """
    if intent not in ("query", "attribution"):
        return "not_query", intent or "non_query"
    if fast_path:
        return "certified", ""
    if no_model or refusal_reason == "no_semantic_match":
        return "unmatched", ("no_model" if no_model else "no_semantic_match")
    meta = compile_meta or {}
    if not meta:
        if plan_validation_status == "dropped":
            return "unplanned", "plan_validation_dropped"
        if last_call_error:
            return "error", "llm_error"
        if llm_calls == 0:
            return "error", "sketch_failed_before_llm"
        return "unplanned", "empty_plan"
    if meta.get("miss_reason") == "no_semantic_layer":
        return "unmatched", "no_semantic_layer"
    outcome = str(meta.get("outcome") or "")
    if outcome == "compiled":
        return "compiled", ""
    if outcome == "partial":
        reasons = [str(r) for r in (meta.get("partial_reasons") or []) if str(r)]
        return "partial", ",".join(reasons)
    if meta.get("miss_class") == "soft":
        return "soft_miss", str(meta.get("miss_reason") or "")
    if refusal_reason:
        return "refused", str(meta.get("miss_reason") or refusal_reason)
    return "unplanned", ("no_plan_intent" if meta.get("plan_typed") else "prose_fallback")


def ratio(num: int, den: int) -> dict[str, Any]:
    """比率三键:分母为 0 → ``rate=None``(不是 0)。

    「无样本」与「算出来是零」是两个可区分的事实,前端据此显示「无法计算」
    而不是 0%(把无样本洗成 0% 是最容易被信以为真的假信号)。
    """
    return {
        "num": int(num),
        "den": int(den),
        "rate": round(num / den, 4) if den else None,
    }


def _empty_counts() -> dict[str, int]:
    return {o: 0 for o in OUTCOMES}


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    """逐题结果 → run 级汇总(确定性;与输入顺序无关)。

    ``results`` 条目只要求 ``outcome`` / ``outcome_reason`` / ``miss_class``
    / ``miss_reason`` 四个键(store 的 result 行或 engine 的内存结果同形)。
    """
    counts = _empty_counts()
    miss_reasons: dict[str, int] = {}
    miss_classes = {"hard": 0, "soft": 0}
    for r in results:
        outcome = str(r.get("outcome") or "error")
        if outcome not in counts:
            outcome = "error"
        counts[outcome] += 1
        reason = str(r.get("miss_reason") or "")
        if reason and outcome in ("partial", "soft_miss", "refused", "unmatched"):
            miss_reasons[reason] = miss_reasons.get(reason, 0) + 1
        klass = str(r.get("miss_class") or "")
        if klass in miss_classes:
            miss_classes[klass] += 1

    total = len(results)
    evaluated = sum(counts[o] for o in EVALUATED_OUTCOMES)
    unmeasured = sum(counts[o] for o in UNMEASURED_OUTCOMES)

    by_miss = [
        {"reason": k, "count": v}
        for k, v in sorted(miss_reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    warnings: list[str] = []
    if total and (counts["unplanned"] + counts["error"]) > HIGH_UNMEASURED_RATIO * total:
        # 未判出占比过高 → 这批结果不能当覆盖率读(单列给出,不自动洗分母)。
        warnings.append("high_unmeasured")

    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "question_total": total,
        "evaluated": evaluated,
        "unmeasured": unmeasured,
        "counts": counts,
        "by_outcome": [
            {"outcome": o, "count": counts[o], "evaluated": o in EVALUATED_OUTCOMES}
            for o in OUTCOMES
        ],
        "by_miss_reason": by_miss,
        "by_miss_class": miss_classes,
        "metrics": {
            "deterministic_coverage": ratio(
                counts["certified"] + counts["compiled"], evaluated),
            "governed_coverage": ratio(
                counts["certified"] + counts["compiled"] + counts["partial"],
                evaluated),
            "gap_rate": ratio(
                counts["soft_miss"] + counts["refused"] + counts["unmatched"],
                evaluated),
            "refusal_rate": ratio(counts["refused"], evaluated),
            "unmatched_rate": ratio(counts["unmatched"], evaluated),
            "passthrough_rate": ratio(counts["soft_miss"], evaluated),
        },
        "warnings": warnings,
    }


def normalize_question(text: str) -> str:
    """问题归一(空白折叠 + casefold)—— diff 的匹配键。"""
    return " ".join(str(text or "").split()).casefold()


def _keyed(questions: list[str]) -> list[tuple[int, str]]:
    """``[(seq, normalized)]``;重复问题按出现序号区分(第 2 个同名 ≠ 第 1 个)。"""
    seen: dict[str, int] = {}
    out: list[tuple[int, str]] = []
    for seq, q in enumerate(questions):
        norm = normalize_question(q)
        n = seen.get(norm, 0)
        seen[norm] = n + 1
        out.append((seq, f"{norm}\x00{n}"))
    return out


def _direction(base: dict[str, Any], current: dict[str, Any]) -> str:
    b, c = str(base.get("outcome") or ""), str(current.get("outcome") or "")
    rb, rc = COVERAGE_RANK.get(b), COVERAGE_RANK.get(c)
    if rb is None or rc is None:
        return "not_comparable"
    if rc > rb:
        return "improved"
    if rc < rb:
        return "regressed"
    if b == c and str(base.get("outcome_reason") or "") == str(
            current.get("outcome_reason") or ""):
        return "same"
    return "changed"


def diff(
    base: dict[str, Any], current: dict[str, Any],
) -> dict[str, Any]:
    """两轮 run 的逐题对比(``base``/``current`` = store 的 run dict)。

    匹配键 = **(归一化问题, 出现序号)** —— 只按问题文本匹配会让重复问题
    互相错位。方向按 :data:`COVERAGE_RANK`;任一侧不在覆盖序里(not_query/
    unplanned/error/skipped)→ ``not_comparable``:把「错误变成错误」说成
    「没变」会给读者一个不该有的安心。
    """
    base_results = list(base.get("results") or [])
    current_results = list(current.get("results") or [])
    base_questions = [str(r.get("question") or "") for r in base_results]
    current_questions = [str(r.get("question") or "") for r in current_results]
    base_index = {k: seq for seq, k in _keyed(base_questions)}
    current_index = {k: seq for seq, k in _keyed(current_questions)}

    questions: list[dict[str, Any]] = []
    matched = 0
    for key, seq in current_index.items():
        bseq = base_index.get(key)
        if bseq is None:
            continue
        matched += 1
        b = base_results[bseq]
        c = current_results[seq]
        questions.append({
            "seq": seq,
            "base_seq": bseq,
            "question": str(c.get("question") or ""),
            "base_outcome": str(b.get("outcome") or ""),
            "outcome": str(c.get("outcome") or ""),
            "base_reason": str(b.get("outcome_reason") or ""),
            "reason": str(c.get("outcome_reason") or ""),
            "change": _direction(b, c),
        })
    questions.sort(key=lambda row: row["seq"])

    changes = {k: 0 for k in ("improved", "regressed", "changed", "same", "not_comparable")}
    for row in questions:
        changes[row["change"]] += 1

    return {
        "matched": matched,
        "base_only": len(base_index) - matched,
        "current_only": len(current_index) - matched,
        "changes": changes,
        "questions": questions,
    }


def summary_delta(
    base_summary: dict[str, Any], current_summary: dict[str, Any],
) -> dict[str, Any]:
    """两轮 summary 的指标差值(rate 缺席任一侧 → delta=None)。"""
    base_metrics = (base_summary or {}).get("metrics") or {}
    current_metrics = (current_summary or {}).get("metrics") or {}
    out: dict[str, Any] = {}
    for name in (
        "deterministic_coverage", "governed_coverage", "gap_rate",
        "refusal_rate", "unmatched_rate", "passthrough_rate",
    ):
        b = base_metrics.get(name) or {}
        c = current_metrics.get(name) or {}
        br, cr = b.get("rate"), c.get("rate")
        out[name] = {
            "base": b,
            "current": c,
            "delta": round(cr - br, 4) if br is not None and cr is not None else None,
        }
    return out
