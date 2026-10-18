"""编译 MISS 分因 → 人话 —— 唯一文案源。

reason slug 是机器语言(compiler 的 ``CompileMiss.reason``)。面向用户的文案
说清「问题里的什么概念没被声明」,slug 只留在日志与契约里。本模块只放纯
常量与纯函数(零 I/O、零依赖,除 ``core.i18n.L``):拒绝节点(refuse.py)、
拒绝报表(api/routers/admin.py)与覆盖率体检(services/question_coverage)
都从这里取同一份文案 —— 三处各写一遍 slug 文案迟早漂移,而「同一个缺口在
三个出口叫三个名字」正是这一层最不能出的错。
"""

from __future__ import annotations

from trove.core.i18n import L

# ── 编译 MISS 分因 → 人话 ────────────────────────────────
# 面向用户的文案说清「问题里的什么概念没被声明」,slug 只留在日志与
# refusal 契约里。``{c}`` 由 component 插值(插值边界见 _TABLE_COPY_REASONS)。
MISS_COPY: dict[str, tuple[str, str]] = {
    "no_metric_match": ("指标(如 {c})", "the metric (e.g. {c})"),
    "metric_anchor_unmatched": ("指标口径", "the metric definition"),
    "unresolved_answer_column": ("查询要输出的字段", "the field the query must output"),
    "unresolved_filter_field": ("筛选条件用到的字段", "the field used by a filter"),
    "enum_value_unresolved": ("筛选值对应的字段", "the field behind a filter value"),
    "missing_filter_value": ("筛选值对应的字段", "the field behind a filter value"),
    "invalid_op": ("比较操作符对应的口径", "the comparison operator's definition"),
    "having_metric_unknown": ("筛选条件用到的指标(如 {c})", "the metric used by a filter (e.g. {c})"),
    "having_without_aggregation": ("筛选条件所依赖的分组口径", "the grouping behind a filter"),
    # 关系类缺口(component = 计划涉及的表名,", ".join):把「涉及哪些表」说出来
    # + 声明指引——A5 报表单列「因关系未声明被拒」的另一半(用户侧可读化)。
    # 注意:只有 component 恒为表名的 reason 才配 {c};ambiguous_join_path 的
    # component 有的是表名、有的是内部诊断串(explicit joins …),加了会泄漏。
    "unknown_cardinality": (
        "表 {c} 之间的关联关系(基数未声明——请在语义模型里声明这两张表如何关联)",
        "how the tables ({c}) relate (cardinality undeclared — declare the relationship in the semantic model)"),
    "fan_out": ("表 {c} 之间的聚合口径(直接联表会重复计数)", "the aggregation path between the tables ({c}) (a plain join double-counts)"),
    "unreachable_table": ("问题涉及的表(与已声明模型不连通)", "the table involved (it is not linked to the declared model)"),
    "table_not_allowed": ("该数据源授权范围外的表", "a table outside this datasource's authorized scope"),
    "ambiguous_join_path": ("表之间唯一的关联路径(存在多条)", "a single join path between the tables (several exist)"),
    "derived_cycle": ("派生指标的循环定义", "a circular derived-metric definition"),
    "derived_depth": ("派生指标的层级(嵌套过深)", "the derived-metric nesting (too deep)"),
    "derived_unresolved": ("派生指标的表达式", "the derived-metric expression"),
    "no_plan_or_matched": ("数据表", "the table"),
    "nothing_compilable": ("数据表与字段", "the tables and fields"),
    "limit_without_order": ("排序口径(要取前 N 条,却没说什么算靠前)", "the ordering (the question asks for the top N but not by what)"),
    "guardrail_rejected": ("计算方式", "the computation"),
    "no_semantic_match": ("概念(指标、字段或筛选口径)", "the concepts (metrics, fields, or filter calibers)"),
    "uncovered": ("概念(指标、字段或筛选口径)", "the concepts (metrics, fields, or filter calibers)"),
}

# 关系类硬 MISS(设计 §5.3,FR-REFUSE-1..2):这四类的 component 携带「本问
# 涉及的表」(或内部诊断串),拒绝出口据此对象化——文案显示名替换、
# related_links 投影、一键补链接。与拒绝报表的单列名单同源(同一份常量)。
# **保持 tuple 而非 set**:报表响应里的 ``reasons`` 列表顺序由此决定,
# 集合迭代顺序不稳定会让同一份数据两次读出不同的字节。
RELATION_MISS_REASONS: tuple[str, ...] = (
    "unknown_cardinality", "fan_out", "ambiguous_join_path", "unreachable_table",
)

# 文案的显示名替换只发生在这两个 reason —— 它们也是「component 恒为表名
# 且 template 带 {c}」的唯二发射点(插值边界见 MISS_COPY 注;另两个关系类
# reason 的 component 可能携带内部诊断串,不插值、也不替换)。
TABLE_COPY_REASONS: frozenset[str] = frozenset({"unknown_cardinality", "fan_out"})

# 无 component 时的通用说法(reason slug → 文案)。只给带 {c} 的四个 reason
# 用 —— label 出口(报表/体检分布)没有单一行级 component 可插值,把
# ``{c}`` 原样留在界面上比缺一个标签更糟。
_GENERIC_COPY: dict[str, tuple[str, str]] = {
    "no_metric_match": ("指标", "the metric"),
    "having_metric_unknown": ("筛选条件用到的指标", "the metric used by a filter"),
    "unknown_cardinality": (
        "表之间的关联关系(基数未声明——请在语义模型里声明这两张表如何关联)",
        "how the tables relate (cardinality undeclared — declare the relationship in the semantic model)"),
    "fan_out": (
        "表之间的聚合口径(直接联表会重复计数)",
        "the aggregation path between the tables (a plain join double-counts)"),
}


def miss_phrase(lang: str, reason: str, component: str = "") -> str:
    """reason slug(+ component)→ 人话;未知 slug 退回泛化说法。

    宁可笼统也不把内部 slug 摆给用户。
    """
    pair = MISS_COPY.get(reason)
    if pair is None:
        return L(lang, "概念(指标、字段或筛选口径)",
                 "the concepts (metrics, fields, or filter calibers)")
    template = pair[0] if lang == "zh" else pair[1]
    return template.replace("{c}", component) if "{c}" in template else template


def miss_label(reason: str) -> dict[str, str]:
    """reason slug → 双语通用标签(无行级 component 可插值的场合)。

    返回 ``{"zh": ..., "en": ...}``;未知 slug 给泛化说法(与 miss_phrase
    同一条「不露 slug」纪律)。带 ``{c}`` 的模板改用 ``_GENERIC_COPY``。
    """
    generic = _GENERIC_COPY.get(reason)
    if generic is not None:
        return {"zh": generic[0], "en": generic[1]}
    pair = MISS_COPY.get(reason)
    if pair is None:
        return {
            "zh": "概念(指标、字段或筛选口径)",
            "en": "the concepts (metrics, fields, or filter calibers)",
        }
    return {"zh": pair[0], "en": pair[1]}
