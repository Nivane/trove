"""SQL 版本链与回归硬检查（Sequential Scaling: 定点修复的保护层）。

版本链记录每轮失败版本（SQL + 结果签名 + 规则命中），注入重生成 prompt
让模型在完整迭代轨迹上做局部修复；回归检查在记录下一版时对比上一版，
产出确定性反馈（无效修复 / 无进展 / 问题转移），封死「复制旧错误」和
「修 A 坏 B」两类迭代退化路径。全部为纯代码，零 LLM。
"""

from __future__ import annotations

from typing import Any


def result_sig(rows: list[list[Any]]) -> str:
    """结果集签名:排序 + 字符串化（与 select/eval 的归一化口径一致）。

    相同签名 = 相同结果集（顺序/类型不敏感）。repr 足够稳定做等值比较。
    """
    return repr(sorted(tuple(str(v) for v in row) for row in rows))


def extract_rule_hits(text: str) -> list[str]:
    """从失败文本提取规则命中（[F1-a] 形式的规则名），保序去重。"""
    import re

    hits = []
    for name in re.findall(r"\[(F\d+-[a-z])\]", text or ""):
        if name not in hits:
            hits.append(name)
    return hits


def record_version(
    existing: list[dict[str, Any]],
    sql: str,
    rows: list[list[Any]],
    issues: list[str],
    round_n: int,
) -> list[dict[str, Any]]:
    """记录本轮失败版本。返回需要追加的条目（不修改 existing）。"""
    if not sql:
        return []
    return [{
        "sql": sql,
        "sig": result_sig(rows),
        "issues": list(issues),
        "round": round_n,
    }]


def regression_report(
    prev: dict[str, Any] | None,
    cur_sig: str,
    cur_issues: list[str],
) -> str | None:
    """对比当前失败与上一版,返回回归报告（None = 有进展,无需额外反馈）。

    三态判定（确定性,按优先级）:
      1. 无效修复: 结果签名相同 → 大概率复制了旧错误（最硬信号）
      2. 无进展:   签名不同但规则命中有交集 → 问题维度未变
      3. 问题转移: 签名不同且规则命中集合变化 → 修 A 引入 B
    无规则命中的失败（执行错误/投票平局）只做签名对比。
    """
    if prev is None:
        return None
    if prev.get("sig") == cur_sig:
        return (
            f"Invalid fix: the result set is identical to Round {prev['round']} "
            f"(the fix likely reproduced the same mistake — do not repeat it)."
        )
    prev_rules = set(prev.get("issues") or [])
    cur_rules = set(cur_issues)
    common = prev_rules & cur_rules
    if common:
        new = cur_rules - prev_rules
        parts = [f"no progress: still violating {', '.join(sorted(common))}"]
        if new:
            parts.append(f"new violations: {', '.join(sorted(new))}")
        return f"Round {prev['round']} → now: " + "; ".join(parts) + "."
    if cur_rules - prev_rules:
        return (
            f"problem shift: Round {prev['round']} violated "
            f"{', '.join(sorted(prev_rules)) or 'another dimension'}, "
            f"now additionally violating {', '.join(sorted(cur_rules - prev_rules))}."
        )
    return None
