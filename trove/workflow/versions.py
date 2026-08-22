"""SQL 版本链与回归硬检查（Sequential Scaling: 定点修复的保护层）。

版本链记录每轮失败版本（SQL + 结果签名 + 规则命中），注入重生成 prompt
让模型在完整迭代轨迹上做局部修复；回归检查在记录下一版时对比上一版，
产出确定性反馈（无效修复 / 无进展 / 问题转移），封死「复制旧错误」和
「修 A 坏 B」两类迭代退化路径。全部为纯代码，零 LLM。

可信度护栏：规则命中分成两类——
  - 可改写类（SQL 自身错误：语法/缺列/值域/范围，改写 SQL 可修复）；
  - 校验冲突类（answer-columns / extra-columns：plan 与结果列的机械对账
    冲突，聚合别名等表达式的合法覆盖被误判，改写 SQL 无法消除）。
签名相同 + 校验冲突类 = 同一个校验器误报复现，不是「复制旧错误」，
回归链不得判 no-progress（否则正确 SQL 会被误伤成优雅降级）。
"""

from __future__ import annotations

from typing import Any

# 校验冲突类规则:这些命中是"S 与计划列名的机械对账"层,聚合表达式/别名
# 的合法写法会被误判,且无法通过改写 SQL 消除——同签名重演时不得判无进展。
VALIDATOR_CONFLICT_RULES = frozenset(
    {"answer-columns", "extra-columns"},
)


def is_validator_conflict(issues: list[str]) -> bool:
    """命中列表是否只由校验冲突类规则构成(是 → 该轮失败与 SQL 正确性无关)。"""
    return bool(issues) and all(i in VALIDATOR_CONFLICT_RULES for i in issues)


def result_sig(rows: list[list[Any]]) -> str:
    """结果集签名:排序 + 字符串化（与 select/eval 的归一化口径一致）。

    相同签名 = 相同结果集（顺序/类型不敏感）。repr 足够稳定做等值比较。
    """
    return repr(sorted(tuple(str(v) for v in row) for row in rows))


def extract_rule_hits(text: str) -> list[str]:
    """从失败文本提取规则命中（[F1-a] 等规则名），保序去重。

    含校验冲突类标签（[answer-columns] / [extra-columns]）：版本链靠它
    区分"可改写的规则失败"与"校验器机械对账冲突"。
    """
    import re

    hits = []
    for name in re.findall(r"\[(F\d+-[a-z]|answer-columns|extra-columns)\]", text or ""):
        if name not in hits:
            hits.append(name)
    return hits


# 执行错误的哨兵签名:本轮 SQL 未执行时结果集签名无意义(rows 为空/过期),
# 用哨兵防止它与真实结果集签名(含空结果 "[]")误撞出 "invalid" 回归判定
EXEC_FAILURE_SIG = "exec-error"


def record_version(
    existing: list[dict[str, Any]],
    sql: str,
    sig: str,
    issues: list[str],
    round_n: int,
    error: str = "",
) -> list[dict[str, Any]]:
    """记录本轮失败版本。返回需要追加的条目（不修改 existing）。

    Args:
        sig: 结果集签名;执行失败(未执行)时传 EXEC_FAILURE_SIG。
        error: 原始失败文本(反馈/理由)——执行错误的"同一失败重演"判定
            以它为准(结果集签名对执行错误无意义)。
    """
    if not sql:
        return []
    return [{
        "sql": sql,
        "sig": sig,
        "issues": list(issues),
        "round": round_n,
        "error": (error or "")[:200],
        # 校验冲突类标志:该轮失败是否只含 answer/extra-columns 类命中。
        # 回归链靠它区分「复制旧错误」vs「校验器误报复现」。
        "validator_conflict": is_validator_conflict(issues),
    }]


def regression_state(
    prev: dict[str, Any] | None,
    cur_sig: str,
    cur_issues: list[str],
) -> str:
    """修复进展量化标签（确定性,置信度信号的数据源）。

    五态判定（按优先级）:
      - first:    无上一版基线（首轮失败）
      - invalid:  结果签名相同 → 大概率复制了旧错误（最硬信号）
      - none:     签名不同但规则命中有交集 → 问题维度未变
      - shift:    签名不同且规则命中集合变化 → 修 A 引入 B
      - improved: 签名变化且无交集无新增 → 有进展
    无规则命中的失败（执行错误/投票平局）只做签名对比。

    校验冲突护栏：上一版只命中了校验冲突类规则（answer/extra-columns）
    且签名相同 → 是"同一个校验器误报复现"而非"复制旧错误"，返回
    ``validator-conflict``（不计入无进展轮次），让正确 SQL 不被误伤降级。
    """
    if prev is None:
        return "first"
    if prev.get("sig") == cur_sig:
        # 上一版失败是校验冲突类(且当前也仍是同类命中)→ 校验器误报复现,
        # 不是"复制旧错误"。签名相同改不了 SQL 也消不掉,不得判无进展。
        if (prev.get("validator_conflict")
                and is_validator_conflict(cur_issues)):
            return "validator-conflict"
        return "invalid"
    prev_rules = set(prev.get("issues") or [])
    cur_rules = set(cur_issues)
    if prev_rules & cur_rules:
        return "none"
    if cur_rules - prev_rules:
        return "shift"
    return "improved"


def regression_report(
    prev: dict[str, Any] | None,
    cur_sig: str,
    cur_issues: list[str],
) -> str | None:
    """对比当前失败与上一版,返回回归报告（None = 有进展,无需额外反馈）。

    三态判定由 regression_state 给出,这里只负责把它翻译成给 LLM 的
    确定性反馈文本（无效修复 / 无进展 / 问题转移）。
    """
    state = regression_state(prev, cur_sig, cur_issues)
    if state in ("first", "improved"):
        return None
    if state == "validator-conflict":
        return (
            f"Validator conflict at Round {prev['round']}: the result set is "
            f"unchanged and the failure is a plan/result column-mapping "
            f"mismatch — this is a validation conflict, not a copied mistake. "
            f"Re-plan the answer columns instead of rewriting the SQL."
        )
    if state == "invalid":
        return (
            f"Invalid fix: the result set is identical to Round {prev['round']} "
            f"(the fix likely reproduced the same mistake — do not repeat it)."
        )
    prev_rules = set(prev.get("issues") or [])
    cur_rules = set(cur_issues)
    if state == "none":
        common = prev_rules & cur_rules
        new = cur_rules - prev_rules
        parts = [f"no progress: still violating {', '.join(sorted(common))}"]
        if new:
            parts.append(f"new violations: {', '.join(sorted(new))}")
        return f"Round {prev['round']} → now: " + "; ".join(parts) + "."
    # state == "shift"
    return (
        f"problem shift: Round {prev['round']} violated "
        f"{', '.join(sorted(prev_rules)) or 'another dimension'}, "
        f"now additionally violating {', '.join(sorted(cur_rules - prev_rules))}."
    )
