"""复杂度分级——纯函数、零 IO/LLM,基于 planner 结构信号 + schema_linking 语义信号。

分级结果驱动负载削减开关:
- "simple"   → gen_sql 走经典子图(跳过 ReAct agent loop)+ 跳过多候选生成;
               validate 规则全过时 reflect 可跳过 LLM 裁决。
- "complex"  → 维持完整链路(ReAct + 多候选 + 裁决),规则链对复杂 SQL
               的语义盲区最大,不值得省。
- "standard" → 默认/证据不足时的保守档,行为与未分级时完全一致。

simple 门槛(2026-08 放宽):≤2 表(允许 join)、可有排序、answer_columns ≤ 3、
聚合 ≤ 2、matched_tables ≤ 2,且必须有术语或 KB 命中(term_hit or kb_hit)。
complex 判据同步调高:≥3 表、子查询迹象、聚合 ≥ 3、plan 被校验丢弃。

判据:plan_json 是 planner 产出的自由格式 JSON(结构信号,最硬);matched_tables
是 schema_linking 的锚定表(语义信号)。所有访问均防御式:缺失/错型键取保守侧
(complex 判据里缺失视为不命中,simple 判据里缺失视为不满足),任何解析问题
只可能把结果推向 standard/complex,不会推向 simple。

保守原则:未知 → standard。plan_json 为 None(planless,planner 失败或未启用)
一律 standard,保证既有行为零漂移。
"""

from __future__ import annotations

import re
from typing import Any

# 子查询/嵌套 SQL 迹象:出现在 plan 的 joins/conditions/aggregation/ordering 文本里。
# 不含 join 关键词——表 join 已允许进 simple(≤2 表),真正的嵌套查询
# 由 select/union/intersect/except/with 捕获。
_SUBQUERY_RE = re.compile(r"\b(select|union|intersect|except|with)\b", re.IGNORECASE)


def _as_list(value: Any) -> list[Any]:
    """把 plan 字段防御式取为 list:None/错型 → []。"""
    if isinstance(value, list):
        return value
    return []


def _as_str(value: Any) -> str:
    """把 plan 字段防御式取为 str:None/错型 → ""。"""
    if isinstance(value, str):
        return value
    return ""


def _text_value(value: Any) -> str:
    """str 原样,list 拼接(dict 取 value/text/note 字段),其余 → "":
    用于子查询迹象的文本扫描。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for v in value:
            if isinstance(v, dict):
                parts.append(" ".join(_as_str(v.get(k)) for k in ("value", "text", "note")))
            else:
                parts.append(_as_str(v))
        return " ".join(parts)
    return ""


def _aggregation_count(plan: dict[str, Any]) -> int:
    """聚合数量:aggregation 为 list 取长度,str 非空计 1;extreme 非空另加 1。"""
    count = 0
    agg = plan.get("aggregation")
    if isinstance(agg, list):
        count += sum(1 for a in agg if _as_str(a))
    elif isinstance(agg, str) and agg.strip():
        count += 1
    extreme = plan.get("extreme")
    if isinstance(extreme, dict) and extreme:
        count += 1
    elif isinstance(extreme, str) and extreme.strip():
        count += 1
    return count


# 各键的合法类型:错型输入(如 tables="students"、joins=42)视为不可信 → standard
_KEY_TYPES: dict[str, type | tuple[type, ...]] = {
    "tables": list,
    "joins": (str, list),
    "conditions": list,
    "aggregation": (str, list),
    "extreme": (str, dict),
    "ordering": str,
    "answer_columns": list,
}


def _well_typed(plan: dict[str, Any]) -> bool:
    """plan 键全部符合预期类型才可信;任何错型 → False(保守降级)。"""
    for key, types in _KEY_TYPES.items():
        if key in plan and not isinstance(plan[key], types):
            return False
    return True


def has_subquery_signal(plan_json: dict[str, Any] | None) -> bool:
    """plan 文本里是否出现嵌套 SQL 迹象(join/子查询/集合运算/CTE 关键词)。"""
    if not plan_json:
        return False
    for key in ("joins", "conditions", "aggregation", "ordering"):
        if _SUBQUERY_RE.search(_text_value(plan_json.get(key))):
            return True
    return False


def grade_complexity(
    plan_json: dict[str, Any] | None,
    matched_tables: list[str] | None = None,
    *,
    term_hit: bool = False,
    kb_hit: bool = False,
    plan_validation: dict[str, Any] | None = None,
) -> str:
    """把 planner 结构信号 + schema_linking 语义信号分级为 simple/standard/complex。

    Args:
        plan_json: planner 的结构化产出(自由 JSON,key: tables/joins/conditions/
            aggregation/extreme/ordering/answer_columns);None → standard。
        matched_tables: schema_linking 锚定的表列表。
        term_hit: 意图分类是否命中术语(KB 命中证据,语义信号)。
        kb_hit: 状态里是否已有 KB 命中记录(如 schema_linking 的 term 命中)。
        plan_validation: planner 的校验结果;status == "dropped" → complex。

    Returns:
        "simple" | "standard" | "complex"
    """
    if not isinstance(plan_json, dict):
        return "standard"
    if not _well_typed(plan_json):
        return "standard"

    # complex 判据(任一命中即 complex;先判,simple 只在全部约束满足时成立)
    tables = [t for t in _as_list(plan_json.get("tables")) if isinstance(t, str) and t]
    if len(tables) >= 3:
        return "complex"
    if has_subquery_signal(plan_json):
        return "complex"
    if _aggregation_count(plan_json) >= 3:
        return "complex"
    if (plan_validation or {}).get("status") == "dropped":
        return "complex"

    # simple 判据(全部满足才 simple)
    if len(tables) > 2:
        return "standard"
    if len(_as_list(plan_json.get("answer_columns"))) > 3:
        return "standard"
    if len(matched_tables or []) > 2:
        return "standard"
    if _aggregation_count(plan_json) > 2:
        return "standard"
    if not (term_hit or kb_hit):
        return "standard"

    return "simple"
