"""缺口归一化 —— 编译 MISS → 人可读、可行动的缺口行(纯函数,零 I/O)。

体检的逐题判定只回答「这题落在哪个结局」;管理者真正要处理的是**缺口**:
「同一个没声明的指标被 23 道题用到」「两张表的关系没声明」。本模块把
``compile_misses`` / 硬 MISS 归一成闭集 kind + 双语标签 + 草稿规格预览 +
工作台深链。

两条纪律:

1. **草稿规格不在这里发明** —— 一律走 ``candidate_specs``(语义草稿的
   唯一候选权威)。能草稿的缺口就是它给得出 spec 的缺口;给不出(关系、
   数据集、表达式类)就只给深链,绝不合成一条「看起来能点」的假草稿。
2. **深链只指向真实存在的管理端落点** —— 与拒绝出口 ``next_actions``
   同一姿态:链接写错比没有链接更糟。

示例::

    >>> kind_for("unknown_cardinality")
    'relation'
    >>> kind_for("no_metric_match")
    'metric'
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from trove.services.semantic_layer.candidates import candidate_specs
from trove.workflow.miss_copy import miss_label

# 缺口类别的闭集(前端按它分组/找图标;新增一类要同时改前端枚举)。
GAP_KINDS: tuple[str, ...] = (
    "metric", "field", "value", "entity", "relation", "dataset", "other",
)

# reason → 缺口类别。与 ``candidate_specs`` 的发射面刻意**不同宽**:分类是
# 「这个缺口属于哪一层建模」(全部 MISS 都要有归属),草稿能力是「机器能否
# 可靠合成一条声明」(只有可解析的指标/字段/值)。两者混成一张表,迟早有人
# 为了让某类显示得好看而把它标成可草稿。
_REASON_KINDS: dict[str, str] = {
    # 指标层
    "no_metric_match": "metric",
    "having_metric_unknown": "metric",
    "metric_anchor_unmatched": "metric",
    # 字段层
    "unresolved_filter_field": "field",
    "unresolved_answer_column": "field",
    "time_field_not_declared": "field",
    "analysis_partition_unresolved": "field",
    "analysis_order_unresolved": "field",
    # 值层
    "missing_filter_value": "value",
    "enum_value_unresolved": "value",
    # 关系层(四个关系类 reason;见 workflow/miss_copy.RELATION_MISS_REASONS)
    "unknown_cardinality": "relation",
    "fan_out": "relation",
    "ambiguous_join_path": "relation",
    "unreachable_table": "relation",
    # 数据集 / 整体未锚定
    "no_semantic_match": "dataset",
    "no_semantic_layer": "dataset",
    "no_model": "dataset",
    # 实体命名(candidate_specs 的元一级候选)
    "entity_naming": "entity",
}

# 深链的 q 只取 component 的第一个表名:关系类 component 形如
# ``loan, account``,整串塞进 ``q`` 在数据集列表里一列都匹配不上。
_MAX_COMPONENT_CHARS = 2000
_MAX_SPECS_PER_GAP = 5


def kind_for(reason: str) -> str:
    """MISS 分因 → 缺口类别;未知分因归 ``other``(绝不丢行)。"""
    return _REASON_KINDS.get(str(reason or ""), "other")


def workbench_link(kind: str, datasource: str, component: str = "") -> str:
    """缺口的 UI 落点(相对路径);没有有意义落点时返回空串。

    - 关系类 → 语义工作台数据集页,``q`` 聚焦第一张表;
    - 数据集类 → KB 页(无模型/未锚定的补救是「去建模」,不是「改某条声明」)。

    其余类别返回空串:指标/字段/值缺口的补救在**本页**的「生成草稿」,
    给一个跳走的工作台链接只会把用户带离他正要做的动作。
    """
    ds = str(datasource or "").strip()
    if not ds:
        return ""
    quoted_ds = quote(ds, safe="")
    if kind == "relation":
        focus = str(component or "").split(",")[0].strip()
        q = f"&q={quote(focus, safe='')}" if focus else ""
        return f"/admin/semantic?ds={quoted_ds}&tab=datasets{q}"
    if kind == "dataset":
        return f"/admin/kb?ds={quoted_ds}"
    return ""


def _clip(text: str, limit: int = _MAX_COMPONENT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _spec_preview(spec: dict[str, Any]) -> dict[str, Any]:
    """草稿规格 → 有界预览(载荷保留原形状,前端据此展示名称/口径)。"""
    return {
        "kind": str(spec.get("kind") or ""),
        "name": str(spec.get("name") or ""),
        "reason": str(spec.get("reason") or ""),
        "evidence": _clip(str(spec.get("evidence") or "")),
        "payload": spec.get("payload") or {},
    }


def make_gap(
    reason: str,
    component: str = "",
    value: str | None = None,
    *,
    model: Any = None,
    datasource: str = "",
) -> dict[str, Any]:
    """单条 MISS → 缺口行。

    ``draftable/specs`` 由当前模型现算(``candidate_specs`` 是纯函数)——
    预览只是**此刻**模型的判断;真正建草稿时会用**那时**的模型再算一遍
    (语义模型可能在体检与草稿之间被编辑过,见 service.draft_gaps)。
    """
    reason = str(reason or "")
    component_text = _clip(str(component or ""))
    miss: dict[str, Any] = {"reason": reason, "component": component_text}
    if value is not None:
        miss["value"] = str(value)
    specs = candidate_specs([miss], model) if model is not None else []
    kind = kind_for(reason)
    return {
        "kind": kind,
        "reason": reason,
        "component": component_text,
        "value": str(value) if value is not None else "",
        "label": miss_label(reason),
        "draftable": bool(specs),
        "specs": [_spec_preview(s) for s in specs[:_MAX_SPECS_PER_GAP]],
        "workbench": workbench_link(kind, datasource, component_text),
    }


def build_gaps(
    misses: list[Any],
    *,
    model: Any = None,
    datasource: str = "",
) -> list[dict[str, Any]]:
    """``compile_misses`` / 硬 MISS → 缺口行列表(保持输入顺序)。"""
    gaps: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for miss in misses or []:
        if not isinstance(miss, dict):
            continue
        reason = str(miss.get("reason") or "")
        component = str(miss.get("component") or "").strip()
        value = miss.get("value")
        key = (reason, component, "" if value is None else str(value))
        if key in seen:
            continue
        seen.add(key)
        gaps.append(make_gap(
            reason, component, value, model=model, datasource=datasource))
    return gaps


def synthetic_dataset_gap(
    reason: str, *, model: Any = None, datasource: str = "",
) -> dict[str, Any]:
    """无单条 MISS 可引用时的合成缺口(non-model / no_semantic_match)。

    合成行**不冒充分因行**:reason 只用 ``no_model`` / ``no_semantic_match``
    / ``no_semantic_layer``,component 为空 —— 管理者需要的是「去建模」的
    入口,不是一条编出来的表名。
    """
    return make_gap(reason, "", None, model=model, datasource=datasource)


def aggregate_gaps(
    results: list[dict[str, Any]],
    *,
    model: Any = None,
    datasource: str = "",
) -> list[dict[str, Any]]:
    """逐题缺口 → 按 ``(kind, reason, component)`` 分组的汇总(顺序稳定)。

    输入顺序无关:输出按 ``count`` 降序 → ``kind`` → ``reason`` →
    ``component`` 定序。``draftable`` 与 ``specs`` **用当前模型重算**
    (落库的逐题行只保存了 run 时的预览;语义模型可能已变 —— 汇总表上
    写着「可生成草稿」而点下去 ``not_draftable`` 是最糟的两种说法)。
    样例问题去重后至多 3 条,``seqs`` 供前端「按组勾选」。
    """
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in results or []:
        if not isinstance(entry, dict):
            continue
        seq = entry.get("seq")
        question = str(entry.get("question") or "")
        for gap in entry.get("gaps") or []:
            if not isinstance(gap, dict):
                continue
            kind = str(gap.get("kind") or "")
            reason = str(gap.get("reason") or "")
            component = str(gap.get("component") or "")
            key = (kind, reason, component)
            bucket = buckets.get(key)
            if bucket is None:
                value = gap.get("value")
                miss: dict[str, Any] = {
                    "reason": reason, "component": component,
                }
                if value:
                    miss["value"] = str(value)
                specs = (
                    candidate_specs([miss], model) if model is not None else []
                )
                bucket = {
                    "kind": kind,
                    "reason": reason,
                    "component": component,
                    "label": miss_label(reason),
                    "count": 0,
                    "draftable": bool(specs),
                    "specs": [
                        _spec_preview(s) for s in specs[:_MAX_SPECS_PER_GAP]
                    ],
                    "workbench": workbench_link(kind, datasource, component),
                    "samples": [],
                    "seqs": [],
                }
                buckets[key] = bucket
            bucket["count"] += 1
            if question and question not in bucket["samples"] and len(
                    bucket["samples"]) < 3:
                bucket["samples"].append(question)
            if isinstance(seq, int) and seq not in bucket["seqs"]:
                bucket["seqs"].append(seq)

    ordered = sorted(
        buckets.values(),
        key=lambda b: (-b["count"], b["kind"], b["reason"], b["component"]),
    )
    for bucket in ordered:
        bucket["seqs"].sort()
    return ordered


__all__ = [
    "GAP_KINDS",
    "aggregate_gaps",
    "build_gaps",
    "kind_for",
    "make_gap",
    "synthetic_dataset_gap",
    "workbench_link",
]
