"""Refuse node — semantic-first (Phase A) rejection + model-extension draft.

Triggered when the semantic model cannot answer the question:
- ``state.no_model`` — the datasource has no initialized semantic model
  (decision 2/3): reject the whole query and prompt the user to run
  ``/kb init`` first. Fully deterministic, zero LLM.
- ``state.refusal`` (compile MISS with a parseable plan): the model is
  missing a declared metric/field. An LLM drafts the minimal extension
  (whitelisted to already-declared datasets), deterministic conflict
  detection runs, and a pending draft lands in ``semantic_drafts.yml``
  for admin confirmation. This turn terminates — nothing is executed.
  After the draft is confirmed, re-asking the same question compiles.

Every refusal also carries ``refusal["next_actions"]`` — a deterministic
list of one-click exits (admin-console deep links) so a dead end is never
a dead end (see the ``next_actions`` section below).

Node shape: ``make_refuse(llm, config, kb=None, semantic_layer=None) ->
async def refuse(state) -> dict``. Returns a partial state update.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from sqlglot import ErrorLevel, exp, parse_one

from trove.core.config import AgentConfig
from trove.core.i18n import L
from trove.llm.gateway import LLMGateway
from trove.services.authz.policy import principal_from_wire
from trove.services.authz.scope import ScopeError, apply_scope
from trove.services.kb.semantic_draft import draft_refusal_extension
from trove.services.semantic_layer.object_graph import build_object_graph
from trove.workflow.miss_copy import (
    MISS_COPY,
    RELATION_MISS_REASONS,
    TABLE_COPY_REASONS,
    miss_phrase,
)
from trove.workflow.state import WorkflowState

logger = logging.getLogger(__name__)


# 拒绝轮**不交付任何查询产物**:交付字段一律清零。
#
# 为什么必须显式清:query_sketch 是回滚目标,前几轮失败可能已经把 sql/rows/
# compiled_sql 写进 state —— 那是**用户从未见过的旧 SQL**。拒绝轮里它若留着:
#   ① output 的 `_answer_source` 以 state.sql 为门,会把旧 SQL 渲染成「本轮答案」
#      的生成 SQL 块,并给出「已认证/已编译」的来源档位;
#   ② `_record_result` 的台账会记下一条从未交付的查询;
#   ③ 评测端(refusal 未归因时)会判在旧 SQL 上,把拒绝误记成一次交付。
# plan / plan_json / compile_meta **保留**:草稿生成要读 plan,miss_reason 是
# 拒绝归因的源。error_feedback 同清 —— 拒绝是终态,在途修正信号没有下一跳。
_HYGIENE: dict[str, Any] = {
    "sql": "",
    "rows": [],
    "columns": [],
    "row_count": -1,
    "execution_time_ms": 0.0,
    "compiled": False,
    "compiled_sql": "",
    "compile_partial": False,
    "compile_misses": [],
    "contract": None,
    "error_feedback": "",
    "plan_replan_pending": False,
}


def _no_model_message(lang: str) -> str:
    return L(
        lang,
        "当前数据源尚未初始化语义模型，无法回答。"
        "请先在管理端对该数据源执行 /kb init 建立语义模型"
        "（冷启动建模是使用前置条件），然后重发此问题。",
        "This datasource has no initialized semantic model, so the question "
        "cannot be answered. Please run /kb init for this datasource in the "
        "admin console first (modeling is a prerequisite to asking), then "
        "re-ask.",
    )


# 编译 MISS 分因文案(MISS_COPY / _miss_phrase / 关系类名单)的唯一权威在
# ``trove/workflow/miss_copy.py`` —— 拒绝节点、拒绝报表与覆盖率体检共用
# 同一份,slug 文案不在此处复制。别名仅为本文件旧引用名保持可读。
_MISS_COPY = MISS_COPY
_RELATION_MISS_REASONS = frozenset(RELATION_MISS_REASONS)
_TABLE_COPY_REASONS = TABLE_COPY_REASONS


# ── 主题域拒绝(确定性,零 LLM)─────────────────────────────
# 三种形态,补救方式各不相同,所以分开说:
#   topic_not_found   —— 选的主题域不存在(前端列表过期/域被删)
#   topic_empty_scope —— 域还在,但它声明的数据集已全部不在模型里(域过期)
#   topic_out_of_scope—— 域有效,但这个问题不在域内(域内零锚定)
_TOPIC_REASONS = {"topic_not_found", "topic_empty_scope"}


def _topic_refusal_reason(refusal: dict[str, Any], reason: str) -> str:
    """refusal → 主题域拒绝分因(非主题域类拒绝返回空串)。

    ``no_semantic_match`` 只在**带主题域上下文**时才升级成 ``topic_out_of_scope``
    —— schema_linking 仅在域生效时把 topic 键写进 refusal,所以这里看到
    topic 键即"这次拒绝发生在域内"。
    """
    if reason in _TOPIC_REASONS:
        return reason
    if reason == "no_semantic_match" and str(refusal.get("topic") or "").strip():
        return "topic_out_of_scope"
    return ""


def _topic_message(lang: str, kind: str, refusal: dict[str, Any]) -> str:
    topic = str(refusal.get("topic") or "")
    topics = [str(t) for t in (refusal.get("available_topics") or []) if str(t)]
    scope = [str(t) for t in (refusal.get("topic_scope") or []) if str(t)]
    listed = ("、".join(topics)) if topics else ""
    listed_en = (", ".join(topics)) if topics else ""
    if kind == "topic_not_found":
        return L(
            lang,
            f"主题域「{topic}」不存在（可能已被改名或删除）。"
            + (f"当前可选主题域:{listed}。" if listed else "")
            + "请在界面上重新选择主题域后重试。",
            f"Topic domain \"{topic}\" does not exist (it may have been renamed "
            "or removed). "
            + (f"Available topics: {listed_en}. " if listed_en else "")
            + "Please pick a topic again and retry.",
        )
    if kind == "topic_empty_scope":
        return L(
            lang,
            f"主题域「{topic}」当前没有可用的数据集(它的声明可能已随模型更新失效)。"
            + (f"当前可选主题域:{listed}。" if listed else "")
            + "请管理员在管理端修正该主题域,或改选其它主题域。",
            f"Topic domain \"{topic}\" currently has no usable datasets (its "
            "declaration may have gone stale as the model changed). "
            + (f"Available topics: {listed_en}. " if listed_en else "")
            + "Ask an admin to fix this topic in the admin console, or pick "
            "another topic.",
        )
    return L(
        lang,
        f"这个问题不在主题域「{topic}」的范围内"
        + (f"(该主题域覆盖:{'、'.join(scope)})" if scope else "")
        + "。请切换到覆盖该问题的主题域"
        + (f"({listed})" if listed else "")
        + ";若该问题确实属于本主题,请管理员把相关数据集并入本主题域。",
        f"This question is outside topic domain \"{topic}\""
        + (f" (its coverage: {', '.join(scope)})" if scope else "")
        + ". Switch to a topic that covers it"
        + (f" ({listed_en})" if listed_en else "")
        + ", or ask an admin to add the relevant datasets to this topic.",
    )


def _miss_phrase(lang: str, reason: str, component: str = "") -> str:
    """未知 slug 退回泛化说法 —— 宁可笼统也不把内部 slug 摆给用户。"""
    return miss_phrase(lang, reason, component)


def _component_tables(component: str) -> list[str]:
    """component → 表名 token 列表(保序去重,大小写无关)。

    component 有三种形态(compiler 发射点):纯表名列表 ``", ".join(matched)``
    (unknown_cardinality / fan_out / BFS 的 ambiguous_join_path)、带前缀的
    表名列表(``tables outside join tree: a, b`` —— 前缀按首个冒号剥掉)、
    内部诊断串(``explicit joins …``)。**含空白(空格/制表)的 token 一律
    丢弃**:标识符不含空白,整句只为诊断而存在 —— 当成表名会让报表多出
    一行伪表(admin 的 ``_by_table`` 同款丢弃,两处同改)。
    """
    text = str(component or "")
    if ":" in text:
        text = text.split(":", 1)[1]
    out: list[str] = []
    seen: set[str] = set()
    for part in text.split(","):
        name = part.strip().strip("`\"'")
        if not name or any(c.isspace() for c in name) or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
    return out


def _entity_display_map(model: Any) -> dict[str, str]:
    """数据集名(小写)→ 实体显示名;只含**已声明实体且显示名非空**的数据集。"""
    if model is None:
        return {}
    names: dict[str, str] = {}
    for d in model.datasets:
        entity = getattr(d, "entity", None)
        display = str(getattr(entity, "display_name", "") or "") if entity is not None else ""
        if display:
            names[str(d.name).lower()] = display
    return names


def _display_mapped_detail(reason_detail: str, model: Any) -> str:
    """关系类文案的显示名替换(设计 §5.3):表名 → 实体显示名。

    边界与 {c} 插值逐字相同:_TABLE_COPY_REASONS 是唯二「component 恒为
    表名且 template 带 {c}」的 reason,只在那里替换;其余 reason(含另两个
    关系类 reason 的内部诊断串、指标/字段名)原样透传。只换**已声明实体**
    的表名,未声明的原样;没有任何 token 变化时逐字节回退旧串(不重组,
    不给未声明模型制造任何字节差)。
    """
    names = _entity_display_map(model)
    if not names:
        return reason_detail
    slug, sep, component = reason_detail.partition(":")
    if not sep or slug.strip() not in _TABLE_COPY_REASONS or not component.strip():
        return reason_detail
    tokens = _component_tables(component)
    if not tokens:
        return reason_detail
    mapped = [names.get(t.lower(), t) for t in tokens]
    if mapped == tokens:
        return reason_detail
    return f"{slug}: {', '.join(mapped)}"


def _related_links(
    model: Any, *, component: str, plan: Any,
) -> list[dict[str, Any]] | None:
    """本问涉及的表/实体之间**已声明**的链接(设计 §5.3 的结构化兄弟键)。

    返回 ``None`` = 这件事投影不出来(键缺席,前端整节不渲染):没有已解析
    的模型,或涉及集与已声明数据集零交集 —— 后者意味着 component 是内部
    诊断串且 plan 也没给出表,投影不出「涉及谁」就不摆一份空断言。
    返回 ``[]`` = 涉及的表确实在声明面上,但两两之间一条已声明链接都没有
    (前端出引导文案)——「投影不出」与「投影出来是零条」是两个可区分的事实。

    数据即 ``build_object_graph`` 投影的过滤(零 I/O):条目 = 两端都在
    涉及集里的已声明边;``from``/``to`` 是端点对象的显示名(D1 回退:实体
    显示名缺省 → 数据集名),``display_name``/``cardinality`` 逐字取自投影
    (列连接串回退不在本层重写第二遍)。
    """
    if model is None:
        return None
    involved = {t.lower() for t in _component_tables(component)}
    if isinstance(plan, dict):
        involved |= {
            str(t).strip().lower()
            for t in (plan.get("tables") or []) if str(t).strip()
        }
    declared = {str(d.name).lower() for d in model.datasets}
    if not (involved & declared):
        return None
    graph = build_object_graph(model)
    display = {
        str(o["dataset"]).lower(): str(o["display_name"]) for o in graph["objects"]
    }
    out: list[dict[str, Any]] = []
    for link in graph["links"]:
        f = str(link["from"]).lower()
        t = str(link["to"]).lower()
        if f in involved and t in involved:
            out.append({
                "name": link["name"],
                "from": display.get(f, link["from"]),
                "to": display.get(t, link["to"]),
                "display_name": link["display_name"],
                "cardinality": link["cardinality"],
            })
    return out


def _draft_name(draft: dict[str, Any]) -> str:
    return str(draft.get("name") or "")


def _draft_expr(draft: dict[str, Any]) -> str:
    return str(draft.get("expression") or "")


def _draft_what(lang: str, draft: dict[str, Any]) -> str:
    """草稿的类别用业务词说:字段 / 指标(而非 kind=field/metric)。"""
    return (
        L(lang, "字段", "field")
        if draft.get("kind") == "field"
        else L(lang, "指标", "metric")
    )


def _uncovered_message(lang: str, reason: str, draft: dict[str, Any] | None,
                       conflict: "Conflict | None" = None,
                       landed: bool = False) -> str:
    """缺声明的用户文案:说清缺什么、你能做什么。

    ``reason`` 可以是编译分因 slug 或 ``slug: component``(refuse 节点拼的),
    一律翻译成人话;YAML 内部键(kind= / expression=)不进用户视野。

    冲突三态:可修正且已落待审(``landed``)→ 收件箱修正;同口径指标
    已存在(agg_duplicate)→ 换问法;其余冲突 → 未写入,人工补充。
    """
    slug, _, component = reason.partition(":")
    component = component.strip()
    what = _miss_phrase(lang, slug.strip(), component)
    if draft is None:
        return L(
            lang,
            f"当前语义模型缺少回答这个问题所需的声明：{what}，"
            "因此暂时无法生成查询。\n"
            "请管理员在管理端的数据源语义模型里补充该声明，然后重新提问。",
            f"The semantic model is missing the declaration this question "
            f"needs: {what}. No query could be built.\n"
            "Ask an admin to add it in the datasource's semantic model, then "
            "ask again.",
        )

    lines: list[str] = []
    if _draft_name(draft):
        lines.append(
            L(
                lang,
                f"- {_draft_what(lang, draft)}名称：{_draft_name(draft)}",
                f"- {_draft_what(lang, draft)} name: {_draft_name(draft)}",
            )
        )
    if _draft_expr(draft):
        lines.append(
            L(
                lang,
                f"- 计算方式：{_draft_expr(draft)}",
                f"- Expression: {_draft_expr(draft)}",
            )
        )
    syns = draft.get("synonyms") or []
    if syns:
        lines.append(
            L(
                lang,
                f"- 识别用词：{', '.join(map(str, syns))}",
                f"- Recognised as: {', '.join(map(str, syns))}",
            )
        )
    body = "\n".join(lines)

    if conflict is not None and conflict.code == "agg_duplicate":
        covered = conflict.name or _draft_name(draft)
        return L(
            lang,
            f"当前语义模型缺少回答这个问题所需的声明：{what}——"
            f"不过系统识别到它已由现有指标「{covered}」覆盖(同一计算方式)。\n"
            f"{body}\n"
            "如果问题里带年份/枚举等条件,请把它们作为筛选条件重新提问"
            "（例：「2020 年的 …」）,不要按值新建指标。",
            f"The semantic model is missing the declaration this question "
            f"needs: {what} — but an existing metric \"{covered}\" already "
            "covers the same computation.\n"
            f"{body}\n"
            "If the question carries a year/enum condition, re-ask with it as "
            "a filter (e.g. \"... in 2020\") instead of creating a per-value "
            "metric.",
        )
    if conflict is not None and landed:
        return L(
            lang,
            f"当前语义模型缺少回答这个问题所需的声明：{what}。\n"
            "系统已自动起草一份补充声明并与现有模型核对,发现一处冲突"
            f"（{_conflict_phrase(lang, conflict)}）,不能直接确认——"
            "草稿已放入待审收件箱,请管理员以它为蓝本修正后新建:\n"
            f"{body}\n",
            f"The semantic model is missing the declaration this question "
            f"needs: {what}.\n"
            "A draft declaration was prepared and checked against the current "
            f"model; it has one conflict ({_conflict_phrase(lang, conflict)}) "
            "and cannot be confirmed directly. It was placed in the review "
            "inbox — an admin can fix it via \"create from this draft\":\n"
            f"{body}\n",
        )
    if conflict is not None:
        return L(
            lang,
            f"当前语义模型缺少回答这个问题所需的声明：{what}。\n"
            "系统尝试自动起草了一份补充声明,但它与现有模型冲突"
            f"（{_conflict_phrase(lang, conflict)}）,因此没有写入:\n"
            f"{body}\n"
            "请管理员在管理端手动补充,然后重新提问。",
            f"The semantic model is missing the declaration this question "
            f"needs: {what}.\n"
            "A draft declaration was attempted but conflicts with the current "
            f"model ({_conflict_phrase(lang, conflict)}), so it was not "
            "written:\n"
            f"{body}\n"
            "Ask an admin to add it manually, then ask again.",
        )
    return L(
        lang,
        f"当前语义模型缺少回答这个问题所需的声明：{what}。\n"
        "系统已自动起草一份补充声明，等待管理员确认：\n"
        f"{body}\n"
        "请到管理端确认这份草稿（确认后会立即重新回答）；确认之前不会生成查询。",
        f"The semantic model is missing the declaration this question needs: "
        f"{what}.\n"
        "A draft declaration was generated and is waiting for review:\n"
        f"{body}\n"
        "Confirm it in the admin console (the question is re-answered right "
        "after); no query runs until then.",
    )


def _auto_confirmed_message(lang: str, reason: str, draft: dict[str, Any] | None) -> str:
    """A/B 档自动确认文案:物理列字段/机械聚合指标已验证入库,正在重答。"""
    local = reason.split(":", 1)[0].strip()
    what = _miss_phrase(lang, local)
    lines: list[str] = []
    if draft:
        if _draft_name(draft):
            lines.append(
                L(
                    lang,
                    f"- {_draft_what(lang, draft)}名称：{_draft_name(draft)}",
                    f"- {_draft_what(lang, draft)} name: {_draft_name(draft)}",
                )
            )
        if _draft_expr(draft):
            lines.append(
                L(
                    lang,
                    f"- 计算方式：{_draft_expr(draft)}",
                    f"- Expression: {_draft_expr(draft)}",
                )
            )
    gate = L(
        lang,
        "该字段已在物理表中确认存在"
        if draft and draft.get("kind") == "field"
        else "该指标已通过编译与真实执行验证",
        "the field was verified against the physical schema"
        if draft and draft.get("kind") == "field"
        else "the metric passed compile + live-execution validation",
    )
    return L(
        lang,
        f"回答这个问题需要{what}，系统已自动补充声明：\n"
        + ("\n".join(lines) + "\n" if lines else "")
        + f"{gate}，已直接写入语义模型，正在重新回答你的问题。",
        f"Answering this needs {what}; the declaration was added automatically:\n"
        + ("\n".join(lines) + "\n" if lines else "")
        + f"{gate} and written to the semantic model — re-answering your question now.",
    )


def _expr_ok(expr: str) -> bool:
    """表达式可解析且不是顶层 Alias(与 manage 的 _check_expr 同判定)。"""
    if not expr or not str(expr).strip():
        return False
    try:
        tree = parse_one(str(expr), read="sqlite", error_level=ErrorLevel.RAISE)
    except Exception:
        return False
    from sqlglot import exp
    return not isinstance(tree, exp.Alias)


@dataclass(frozen=True)
class Conflict:
    """草稿与现有模型的确定性冲突。

    ``code`` 是机器可读分因(报表/文案映射用);``message`` 是中文诊断
    (日志、草稿注解、管理员视野——**不进用户文案**,用户文案按 code 走
    ``_CONFLICT_COPY`` 双语映射)。``landable``:冲突可人工修正(重名 /
    表达式不可解析 / 数据集引用错)→ 内容照落 pending 待审;
    不可修正类(名称缺失 / 未知类型)只展示不落库。
    """

    code: str
    message: str
    landable: bool = False
    #: 冲突涉及的既有资产名(agg_duplicate 的既有指标名;文案插值用)。
    name: str = ""


# 冲突 code → 用户可见短语(双语)。**不暴露 message 原文**:它是中文诊断,
# 英文界面会漏 zh;code 是封闭集,漏了退回泛化说法。
_CONFLICT_COPY: dict[str, tuple[str, str]] = {
    "expr_unparseable": ("声明的计算方式无法解析", "the proposed expression does not parse"),
    "name_declared": ("名称与现有声明重名", "the name duplicates an existing declaration"),
    "dataset_undeclared": ("引用了未声明的数据集", "it references an undeclared dataset"),
    "field_dataset_undeclared": ("它所在的数据集未声明", "its dataset is not declared"),
    "field_declared": ("该字段已声明", "the field is already declared"),
    "name_missing": ("缺少名称", "it has no name"),
    "field_name_form": ("字段名不是 dataset.field 形式", "the field name is not in dataset.field form"),
    "agg_duplicate": ("已有同口径指标", "an equivalent metric already exists"),
    "unknown_kind": ("草稿类型未知", "the draft kind is unknown"),
    "entity_declared": ("该数据集已声明实体（确认将覆盖现有声明）",
                        "the dataset already declares an entity (confirming overwrites it)"),
}


def _conflict_phrase(lang: str, conflict: "Conflict") -> str:
    pair = _CONFLICT_COPY.get(conflict.code)
    if pair is None:
        return L(lang, "与现有模型冲突", "it conflicts with the current model")
    return L(lang, pair[0], pair[1])


def _detect_declaration_conflict(
    model, draft: dict[str, Any], declared_datasets: set[str],
) -> Conflict | None:
    """实体/关系声明草稿(E2)的冲突检测。

    两类可修正冲突(内容照落 pending + 确认被守卫拦住,以蓝本新建后
    upsert 覆盖):

    * ``entity``:目标数据集已声明实体 → ``entity_declared``(重名声明
      与 agg_duplicate 不同 —— 覆盖一条实体声明是**有意义的修正路径**,
      所以落库 + 给蓝本动作,而不是既不落库也不给动作);
    * ``relationship``:关系名重复 → ``name_declared``(与 metric 同款)。

    引用未声明数据集 → ``dataset_undeclared``(与 field 分支同款);
    空名 → ``name_missing``。**from/to 缺失不在这里发明冲突码**:
    它是「草稿本身结构不全」,交给 ``_apply_relationship`` 在确认时响亮
    拒绝(冲突码闭集见 ``_CONFLICT_COPY``,不临时加词)。
    """
    payload = draft.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    kind = draft.get("kind")
    if kind == "entity":
        name = str(draft.get("name") or payload.get("dataset") or "").strip()
        if not name:
            return Conflict("name_missing", "实体声明缺少归属数据集名")
        if name not in declared_datasets:
            return Conflict(
                "dataset_undeclared", f"数据集「{name}」未声明", landable=True)
        ds = next(d for d in model.datasets if d.name == name)
        if ds.entity is not None:
            return Conflict(
                "entity_declared",
                f"数据集「{name}」已声明实体——确认将覆盖现有声明",
                landable=True, name=name)
        return None
    name = str(draft.get("name") or "").strip()
    if not name:
        return Conflict("name_missing", "关系名缺失")
    if model is not None and any(r.name == name for r in model.relationships):
        return Conflict("name_declared", f"关系「{name}」已声明", landable=True)
    endpoints = [str(payload.get("from") or "").strip(),
                 str(payload.get("to") or "").strip()]
    undeclared = [t for t in dict.fromkeys(e for e in endpoints if e)
                  if t not in declared_datasets]
    if undeclared:
        return Conflict(
            "dataset_undeclared", f"引用了未声明的数据集 {undeclared}", landable=True)
    return None


def _detect_conflict(model, draft: dict[str, Any]) -> Conflict | None:
    """确定性冲突检测:同名定义 / 表达式不可解析 / 数据集未声明。

    返回 ``None`` = 无冲突可写库。code 封闭集见 ``_CONFLICT_COPY``;
    ``agg_duplicate`` 是"已有同聚合签名指标"——同名不同名的都算,
    落草稿=诱导确认一条必然重复的指标,所以既不落库也不给建档动作
    (补救是换问法加过滤条件,不是补声明)。
    """
    kind = draft.get("kind")
    declared_datasets = {d.name for d in model.datasets} if model is not None else set()
    # 实体/关系草稿(E2)没有表达式,数据在 payload 里(前端对话框的自有形状);
    # 必须在 _expr_ok 之前分流,否则一律被当成"表达式不可解析"。
    if kind in ("entity", "relationship"):
        return _detect_declaration_conflict(model, draft, declared_datasets)
    expr = str(draft.get("expression") or "").strip()
    if not _expr_ok(expr):
        return Conflict("expr_unparseable", "表达式不可解析", landable=True)
    declared_metrics = {m.name for m in model.metrics} if model is not None else set()
    if kind == "metric":
        name = str(draft.get("name") or "").strip()
        if not name:
            return Conflict("name_missing", "指标名缺失")
        if name in declared_metrics:
            return Conflict("name_declared", f"指标「{name}」已声明", landable=True)
        refs = draft.get("datasets") or []
        if refs and any(d not in declared_datasets for d in refs):
            bad = [d for d in refs if d not in declared_datasets]
            return Conflict(
                "dataset_undeclared", f"引用了未声明的数据集 {bad}", landable=True)
        # 表达式去重:草稿指标与已有指标聚合签名兼容(如 COUNT(loan.loan_id)
        # 与既有 loan_count 系列)→ 视为重复定义。年份/枚举值应是过滤条件,
        # 不是指标本体——拒绝把「loan_count_in_2020」这类按值拆分的指标入库。
        if model is not None:
            from trove.services.semantic_layer.compiler import (
                _agg_signature,
                _sig_compatible,
            )
            draft_sig = _agg_signature(expr)
            if draft_sig is not None:
                for m in model.metrics:
                    m_sig = _agg_signature(m.expression)
                    if m_sig is not None and _sig_compatible(draft_sig, m_sig):
                        return Conflict(
                            "agg_duplicate",
                            f"已有同表达式指标「{m.name}」({m.expression})"
                            "——时间/枚举等过滤值应是查询条件,请勿按值新建指标",
                            name=str(m.name))
        return None
    if kind == "field":
        name = str(draft.get("name") or "").strip()
        ds_name, sep, field_name = name.partition(".")
        if not sep:
            return Conflict("field_name_form", "字段名必须是 dataset.field 形式")
        if ds_name not in declared_datasets:
            return Conflict(
                "field_dataset_undeclared", f"数据集「{ds_name}」未声明", landable=True)
        ds = next(d for d in model.datasets if d.name == ds_name)
        if any(f.name == field_name for f in ds.fields):
            return Conflict(
                "field_declared", f"字段「{name}」已声明", landable=True)
        return None
    return Conflict("unknown_kind", "未知的草稿类型")


def _payload_for(kind: str, draft: dict[str, Any]) -> dict[str, Any]:
    """refuse draft → SemanticManager.create_draft 的 payload(与 confirm 对齐)。"""
    payload: dict[str, Any] = {"expression": str(draft.get("expression") or "").strip()}
    syns = [s for s in (draft.get("synonyms") or []) if str(s).strip()]
    if syns:
        payload["synonyms"] = syns
    definition = str(draft.get("definition") or "").strip()
    if kind == "field":
        if definition:
            payload["description"] = definition
        if draft.get("datatype"):
            payload["datatype"] = str(draft["datatype"])
    elif definition:
        payload["definition"] = definition
    if kind == "metric" and draft.get("datasets"):
        payload["datasets"] = list(draft["datasets"])
    return payload


async def _physical_column_field(
    connectors: Any, datasource: str, model, draft: dict[str, Any],
) -> str:
    """A 档:字段草稿的表达式是否对应物理表中的真实列。

    通过 → 返回 ``dataset.field`` 名(可直接自动确认);否则返回 ""。
    判定:kind=field、数据集已声明、表达式去掉表限定后命中该数据集
    source 表的真实列(不涉及聚合/计算列——那些仍走人工)。catalog
    不可用/异常 → 保守返回 ""(退回 pending 草稿,不猜)。
    """
    if connectors is None or model is None or not datasource:
        return ""
    if draft.get("kind") != "field":
        return ""
    name = str(draft.get("name") or "").strip()
    ds_name, sep, field_name = name.partition(".")
    if not sep or not ds_name or not field_name:
        return ""
    ds = next((d for d in model.datasets if d.name == ds_name), None)
    if ds is None:
        return ""
    expr = str(draft.get("expression") or "").strip()
    if not expr:
        return ""
    bare = expr.rsplit(".", 1)[-1].strip("`\"'")
    if not bare or bare.lower() in {f.name.lower() for f in ds.fields}:
        # 已声明字段名重复被 _detect_conflict 拦;此处再防一道(改名映射)
        return ""
    physical_table = str(ds.source or ds_name).rsplit(".", 1)[-1]
    try:
        schema = await connectors.get_schema(datasource)
    except Exception as e:
        # Fail-closed (the draft stays pending) — but a catalog outage is an
        # environmental fact, not a verdict on the draft; leave a trace.
        logger.debug("field draft auto-confirm skipped (schema unavailable): %s", e)
        return ""
    for t in schema.tables:
        if t.name.lower() != physical_table.lower():
            continue
        if any(c.name.lower() == bare.lower() for c in t.columns):
            return name
    return ""


# A 档自动确认单问上限:连补几个物理列字段仍编译失败 → 停止自动,回退人工。
AUTO_CONFIRM_MAX_ROUNDS = 2

# B 档:机械聚合白名单(值无关 COUNT/SUM/AVG)。派生/比率/复杂口径仍走人工。
_MECHANICAL_AGGS = {"count", "sum", "avg"}
# 机械聚合执行护栏:超大表跳过真实执行(避免重聚合成本),退回人工。
_AUTO_EXEC_MAX_ROWS = 2_000_000
_AUTO_EXEC_TIMEOUT_S = 5


async def _mechanical_metric_ok(
    connectors: Any, datasource: str, model, draft: dict[str, Any],
    *, principal: Any = None,
) -> str:
    """B 档:机械聚合指标(COUNT/SUM/AVG 值无关)的确定性验证门。

    通过 → 返回 ``dataset 名``(可直接自动确认);否则返回 ""。
    验证门 = 编译通过 + 表可达 + _agg_signature 兼容 + 真实执行(shape 规则):

      1. 表达式是**单个**机械聚合(count/sum/avg)——多聚合算式不算——且值无关
         (无 FILTER/WHERE/子查询把过滤值写死进指标);
      2. 单数据集、已声明、物理表存在(表可达),引用列在物理表中存在;
      3. 把草稿指标临时并入模型副本,走权威编译器 compile_detailed
         + guardrail(编译通过、投影表守卫、签名兼容)——编译器 MISS 即拒;
      4. 用物理表名重写表达式,在数据源真实执行,结果过 rules.verify
         shape 链(单行单列标量)。

    任一不过 → 退回 pending 走人工(保守,不靠 LLM 自评)。

    ``principal`` 是提问者的授权主体(设计 §2.3):第 4 步是**真实执行**,
    必须与 ``execute_sql`` 同一道行/列门。``result.sql`` 出自语义编译器
    (声明层 RLS 已注入),所以 ``include_declared_rls=False``;``ScopeError``
    视为**试跑失败**——自动确认跳过,退回人工,绝不执行越权 SQL 换取一个
    自动确认。
    """
    if connectors is None or model is None or not datasource:
        return ""
    if draft.get("kind") != "metric":
        return ""
    name = str(draft.get("name") or "").strip()
    expr = str(draft.get("expression") or "").strip()
    if not name or not expr:
        return ""
    from trove.services.semantic_layer.compiler import (
        CompileResult,
        SemanticCompiler,
        _agg_signature,
        validate_compiled_sql,
    )
    from trove.services.semantic_layer.models import SemanticMetric, SemanticModel

    # 1) 机械聚合 + 值无关
    # 签名是「全量聚合」元组(见 compiler._agg_signature):单聚合才是机械性的
    # 必要条件——SUM(a)/COUNT(b) 这类算式不是值无关的机械聚合,自动确认会放过
    # 一个语义未经人审的派生度量。
    sig = _agg_signature(expr)
    if sig is None or len(sig) != 1 or sig[0][0] not in _MECHANICAL_AGGS:
        return ""
    try:
        tree = parse_one(expr, error_level=ErrorLevel.RAISE)
    except Exception:
        return ""
    if (tree.find(exp.Filter) or tree.find(exp.Where) or tree.find(exp.Subquery)
            or tree.find(exp.Case)):
        return ""  # 值被写死进指标(CASE/WHERE/FILTER)→ 不是值无关机械聚合

    # 2) 单数据集 + 表可达 + 引用列存在
    datasets = [str(d) for d in (draft.get("datasets") or []) if str(d).strip()]
    if not datasets:
        return ""
    ds_name = datasets[0]
    declared = {d.name for d in model.datasets}
    if len(set(datasets)) != 1 or ds_name not in declared:
        return ""
    ds = next(d for d in model.datasets if d.name == ds_name)
    physical_table = str(ds.source or ds_name).rsplit(".", 1)[-1]
    try:
        schema = await connectors.get_schema(datasource)
    except Exception as e:
        logger.debug("metric draft auto-confirm skipped (schema unavailable): %s", e)
        return ""
    tbl = next((t for t in schema.tables if t.name.lower() == physical_table.lower()), None)
    if tbl is None or (tbl.row_count_estimate or 0) > _AUTO_EXEC_MAX_ROWS:
        return ""
    phys_cols = {c.name.lower() for c in tbl.columns}
    sig_cols = {c.rsplit(".", 1)[-1].lower() for c in sig[0][1]}
    if sig_cols - phys_cols:
        return ""  # 引用列不在物理表 → 表不可达/列不存在

    # 3) 编译通过 + guardrail:草稿指标并入模型副本,走权威编译通道
    probe = SemanticModel(
        name=model.name,
        datasets=list(model.datasets),
        relationships=list(model.relationships),
        metrics=list(model.metrics) + [
            SemanticMetric(name=name, expression=expr, datasets=[ds_name])],
    )
    plan = {
        "tables": [ds_name],
        "aggregation": name,
        "answer_columns": [name],
        "conditions": [],
    }
    try:
        adapter = await connectors.get(datasource)
        dialect = adapter.dialect() or "sqlite"
    except Exception:
        dialect = "sqlite"
    try:
        result = SemanticCompiler(probe).compile_detailed(
            plan, [ds_name], force_dialect=dialect)
        # B 档自动确认要求**完整编译**(软 MISS 骨架不算——机械聚合指标必须
        # 全解析才可免人工入库)。
        if not isinstance(result, CompileResult):
            return ""
        if validate_compiled_sql(result.sql, probe, [ds_name]):
            return ""
    except Exception:
        return ""

    # 4) 真实执行 + shape 规则(单行单列标量):执行编译器产出的权威 SQL
    #    (与生产 execute_sql 同一 SQL,忠实验证「这条指标能跑出标量」)。
    #    执行前过行/列 scope —— 试跑也是**用户触发的真实执行**。
    exec_sql = result.sql
    if principal is not None:
        try:
            exec_sql = apply_scope(
                result.sql, model=probe, principal=principal,
                datasource=datasource, dialect=dialect,
                include_declared_rls=False,
            ).sql
        except ScopeError:
            return ""  # 试跑越权 = 试跑失败 → 退回 pending 人工
    from trove.workflow.rules import verify
    try:
        res = await asyncio.wait_for(
            connectors.execute(exec_sql, datasource), timeout=_AUTO_EXEC_TIMEOUT_S)
    except Exception as e:
        logger.debug("metric draft auto-confirm trial execution failed: %s", e)
        return ""
    if not res.columns or not res.rows:
        return ""
    reason, _hits = verify(
        "what is the " + sig[0][0] + "?",
        result.sql, res.columns, res.rows, len(res.rows), lang="en")
    if reason:
        return ""
    return ds_name


# ── 拒绝出口(next_actions)────────────────────────────────
# 拒绝不该是死胡同:每条拒绝都随 refusal 附一份**一键下一步**,形状统一为
#   [{"id", "kind", "label", "href", "admin_only", "payload"?}]
# 后端给全(含提问语言的 label),前端只渲染、不分类 —— 与错误呈现层同纪律
# (frontend/utils/errors.ts:「前端绝不自己分类,后端给、前端只渲染」)。
# 缺键/空列表 = 这条拒绝没有出口(前端按缺席处理,不替它编一个)。
#
# ``admin_only`` 为什么显式给:拒绝文案非管理员也看得到(「请管理员…」),
# 而动作落点全在管理端 —— 由前端拿登录角色过闸,分类判断留在判定侧。
# 不做「用户侧一键触发 /kb init」:建模是管理端行为(冷启动建模是前置条件),
# 非管理员的正确出口是这句话本身,不是一个点了会 403 的按钮。

def _admin_action(
    lang: str, kind: str, href: str, zh: str, en: str,
    *, payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "id": kind,
        "kind": kind,
        "label": L(lang, zh, en),
        "href": href,
        "admin_only": True,
    }
    if payload:
        action["payload"] = payload
    return action


def _semantic_url(datasource: str, *, tab: str = "") -> str:
    """语义工作台深链:SemanticView 的 URL 状态键是 ``ds`` / ``tab``
    (useListQuery),不是旧式的 ``?pending=1``(那个键没人读)。"""
    parts = []
    if datasource:
        parts.append(f"ds={quote(datasource)}")
    if tab:
        parts.append(f"tab={tab}")
    return "/admin/semantic" + (f"?{'&'.join(parts)}" if parts else "")


def _id_names(model: Any, dataset: str) -> dict[str, str]:
    """数据集声明的 ``*_id`` 列名(小写 → 原拼写),含主键/唯一键引用。

    命名口径与 metadata 路径的 ``answer._join_hints`` 同款:
    ``endswith("_id")`` 且长度 > 3(排除赤裸的 ``id``/``xx`` 之类噪声)。
    """
    ds = next((d for d in model.datasets if str(d.name).lower() == dataset.lower()), None)
    if ds is None:
        return {}
    names: list[str] = [str(f.name) for f in ds.fields]
    for extra in (ds.primary_key, *(ds.unique_keys or [])):
        names.extend(str(c) for c in extra if c not in names)
    out: dict[str, str] = {}
    for n in names:
        s = n.strip()
        if len(s) > 3 and s.lower().endswith("_id"):
            out.setdefault(s.lower(), s)
    return out


def _relation_columns(
    model: Any, from_dataset: str, to_dataset: str,
) -> tuple[list[str], list[str]]:
    """唯一确定性 ``*_id`` 名称匹配 → 两端列对;零匹配/多匹配 → 空(机器不猜列)。

    「唯一」是硬条件:两个名字都像连接键时挑哪个都是猜,错键的关系声明
    会静默产出错结果(所以 A2 决策 (d) 拒了自动落关系草稿——预填列同理)。
    """
    left = _id_names(model, from_dataset)
    right = _id_names(model, to_dataset)
    shared = [k for k in left if k in right]
    if len(shared) != 1:
        return [], []
    return [left[shared[0]]], [right[shared[0]]]


def _new_relation_action(
    lang: str, datasource: str, model: Any, component: str, reason: str,
) -> dict[str, Any] | None:
    """关系类 MISS 的「新建关系」蓝本(next_actions 新 kind,设计 §5.3)。

    只在机器**确定**两端点时才给:component 恰两个 token 且都大小写无关
    命中已声明数据集;否则不给动作(机器不猜表对)。列对仅在唯一 ``*_id``
    名称匹配时预填,否则留空。**不自动落草稿**——落点是语义台的关系编辑
    对话框,确认门照旧(FR-REFUSE-2 的「打开即见预填好的关系声明草稿」
    兑现为对话框预填,草稿由人提交)。

    payload 键名与前端 Blueprint 契约一致(``from_dataset``/``to_dataset``/
    ``from_columns``/``to_columns``,NextActions 机械转运为 ``bp_*``);
    ``reason`` 是原始 miss slug(管理端诊断用,前端不认识就忽略)。
    """
    if model is None or not datasource:
        return None
    tokens = _component_tables(component)
    if len(tokens) != 2:
        return None
    by_lower = {str(d.name).lower(): str(d.name) for d in model.datasets}
    endpoints = [by_lower.get(t.lower()) for t in tokens]
    if not all(endpoints):
        return None
    from_columns, to_columns = _relation_columns(model, endpoints[0], endpoints[1])
    payload: dict[str, Any] = {
        "draft_kind": "relationship",
        "from_dataset": endpoints[0],
        "to_dataset": endpoints[1],
        "reason": reason,
    }
    if from_columns:
        payload["from_columns"] = from_columns
        payload["to_columns"] = to_columns
    return _admin_action(
        lang, "new_relation_from_miss", _semantic_url(datasource),
        "声明关系（已预填两端）", "Declare the relationship (both ends prefilled)",
        payload=payload,
    )


def _blueprint_payload(draft: dict[str, Any],
                       conflict: "Conflict | None" = None) -> dict[str, Any]:
    """冲突草稿的蓝本内容:前端「以此为蓝本新建」预填用(值原样透传,
    都是已经展示在拒绝文案里的字段,没有新的暴露面)。``conflict`` 带上
    分因 code/中文诊断,对话框展示"为什么不能直接确认"。"""
    out: dict[str, Any] = {
        "draft_kind": str(draft.get("kind") or ""),
        "draft_name": str(draft.get("name") or ""),
        "expression": str(draft.get("expression") or ""),
    }
    if draft.get("datasets"):
        out["datasets"] = list(draft["datasets"])
    payload = draft.get("payload")
    if isinstance(payload, dict) and str(draft.get("kind") or "") == "relationship":
        # 关系草稿的声明内容在 payload 里(前端对话框形状);键名与前端
        # Blueprint 契约一致(from_dataset/to_dataset/from_columns/
        # to_columns,NextActions 机械转运为 bp_*)。
        out["from_dataset"] = str(payload.get("from") or "")
        out["to_dataset"] = str(payload.get("to") or "")
        out["from_columns"] = [str(c) for c in (payload.get("from_columns") or [])]
        out["to_columns"] = [str(c) for c in (payload.get("to_columns") or [])]
    if conflict is not None:
        out["conflict_code"] = conflict.code
        if conflict.message:
            out["conflict_message"] = conflict.message
    return out


def _no_model_actions(lang: str, datasource: str) -> list[dict[str, Any]]:
    """无语义模型 → 「去建档」。落点 = 该数据源的 KB 页:init 的 202+轮询
    流程在那里(不自动触发——一条链接不该悄悄烧掉一次 LLM 建档)。"""
    ds = str(datasource or "")
    href = f"/admin/kb?ds={quote(ds)}" if ds else "/admin/kb"
    return [_admin_action(
        lang, "datasource_init", href,
        "去初始化语义模型", "Initialize the semantic model",
    )]


def _draft_actions(
    lang: str, datasource: str, *,
    draft: dict[str, Any] | None, conflict: "Conflict | None",
    entry: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """编译 MISS 拒绝的出口(与 refusal 的 draft/conflict/entry 对齐):

      draft=None                → 起草就没成,工作台是唯一去处
      agg_duplicate             → 不是缺声明(是已有同口径指标)→ 无控制台动作
                                  (补救=换问法,文案已说清)
      conflict(其余)            → 不能直接确认(落了待审也只可作蓝本)→
                                  「以这份草稿为蓝本新建」(内容随 payload 预填;
                                  草稿在收件箱时深链带 tab=pending)
      entry=None                → 草稿只展示了、没落库(无 kb/数据源上下文)→ 同上
      entry≠None                → 草稿已落 pending 收件箱 → 确认(确认后重答)

    A/B 档自动确认成功走的是**早退分支**(refusal=None,路由回重答),
    不在这里——没有需要人做的一步,就不该给动作。
    """
    ds = str(datasource or "")
    if draft is not None and conflict is not None:
        if conflict.code == "agg_duplicate":
            return []
        return [_admin_action(
            lang, "new_from_draft",
            _semantic_url(ds, tab="pending") if entry is not None else _semantic_url(ds),
            "以这份草稿为蓝本新建", "Create from this draft",
            payload=_blueprint_payload(draft, conflict),
        )]
    if draft is None or entry is None:
        return [_admin_action(
            lang, "open_semantic", _semantic_url(ds),
            "去语义工作台补充声明", "Add the declaration in the semantic workbench",
        )]
    payload: dict[str, Any] = {
        "draft_kind": str(draft.get("kind") or ""),
        "draft_name": str(draft.get("name") or ""),
    }
    if entry.get("id"):
        payload["draft_id"] = str(entry["id"])
    return [_admin_action(
        lang, "confirm_draft", _semantic_url(ds, tab="pending"),
        "去确认这份草稿(确认后自动重答)", "Confirm the draft (the question re-answers)",
        payload=payload,
    )]


def make_refuse(
    llm: LLMGateway,
    config: AgentConfig,
    kb: Any | None = None,
    semantic_layer: Any | None = None,
    connectors: Any | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the refuse node bound to an LLM gateway + semantic services.

    Args:
        llm: LLM gateway (used only for the uncovered-draft drafting).
        config: Agent config (model selection / language).
        kb: Optional KbService — enables writing the pending draft to
            ``semantic_drafts.yml`` for the admin confirm flow.
        semantic_layer: Optional live semantic provider — supplies the
            current model for conflict detection.
        connectors: Optional ConnectorRegistry — A 档自动确认的物理 schema
            验证源:字段草稿命中真实列 → 直接入库重答,无需人工。
    """

    async def refuse(state: WorkflowState) -> dict[str, Any]:
        # Upstream failure — pass through
        if state.error:
            return {}

        if state.no_model:
            message = _no_model_message(state.lang)
            datasource = state.datasource or ""
            return {
                "clarification_question": message,
                "refusal": {
                    "reason": "no_model",
                    "question": state.question,
                    # datasource 是出口深链的原料(前端拼不出「去哪个源建档」);
                    # 键先于 next_actions 存在——旧消费者读 reason/message 不受影响。
                    "datasource": datasource,
                    "message": message,
                    "next_actions": _no_model_actions(state.lang, datasource),
                },
                **_HYGIENE,
            }

        refusal = state.refusal or {}
        if not refusal:
            return {}

        reason = str(refusal.get("reason") or "未覆盖")
        # 主题域类拒绝:**确定性短路**,不走草稿机。
        # 这几种拒绝的补救方式与"模型缺声明"完全不同(换域 / 把数据集并进
        # 域 / 管理员修主题域声明),而且让它去起草 metric/field 是答非所问
        # —— 域的边界不是"缺了一个声明"。
        topic_reason = _topic_refusal_reason(refusal, reason)
        if topic_reason:
            message = _topic_message(state.lang, topic_reason, refusal)
            # 主题域拒绝**不带 next_actions**:补救动作是「换域重问」——
            # 就在提问界面的主题选择器里(在页动作,不是管理端深链);
            # 域声明本身没有管理端页面(在 semantics.yml 里)。
            return {
                "auto_confirmed": False,
                "clarification_question": message,
                "refusal": {
                    "reason": reason,
                    "question": str(refusal.get("question") or state.question),
                    "datasource": state.datasource or "",
                    "message": message,
                    "topic": str(refusal.get("topic") or ""),
                    "available_topics": list(refusal.get("available_topics") or []),
                },
                **_HYGIENE,
            }
        # question/plan/model 先解析:关系类拒绝的显示名替换、related_links
        # 投影与「新建关系」蓝本都要用已解析的模型 —— 模型本就在本节点解析
        # (N23),这里只是把它提到文案构造之前,不新增 I/O 面。
        question = str(refusal.get("question") or state.question)
        plan = refusal.get("plan")
        model = None
        if semantic_layer is not None:
            try:
                model = semantic_layer.model()
            except Exception as e:
                logger.warning("Semantic model lookup failed (%s): %s",
                               state.datasource, e)
                model = None
        # 编译 MISS 的结构化分因(reason slug + 失败组件)→ 拼进用户可见
        # 文案,管理端知道具体缺哪个声明(metric/字段/join),而不是笼统
        # 「uncovered」。返回的 refusal["reason"] 保持原始值(上游契约/
        # 机器匹配不变),派生 detail 只进 message。
        #
        # 文案的 reason **直接用分因 slug**(不是 "uncovered (slug: …)" 的
        # 包装形)——_MISS_COPY 表按 slug 查短语,包装形在 first-colon 切分
        # 后就查不中、整表退化成泛化说法(A2 修正;关系类缺口因此才说得出
        # 「涉及哪两张表」:_MISS_COPY 的 {c} 由 component 插值)。
        reason_detail = reason
        cm = refusal.get("compile_miss") or {}
        if isinstance(cm, dict) and cm.get("reason"):
            detail = str(cm["reason"])
            if cm.get("component"):
                detail = f"{detail}: {cm['component']}"
            reason_detail = detail
        # 关系类硬 MISS 的出口原料(设计 §5.3):显示名替换只作用于**文案
        # 副本**(copy_detail)——refusal 的 compile_miss 保持原始分因(审计
        # miss_component / 报表 by_table 按原文聚合,替换只活在文案里)。
        miss_reason = str(cm.get("reason") or "") if isinstance(cm, dict) else ""
        miss_reason = miss_reason or reason
        relation_miss = miss_reason in _RELATION_MISS_REASONS
        copy_detail = (
            _display_mapped_detail(reason_detail, model) if relation_miss
            else reason_detail
        )
        component = str(cm.get("component") or "") if isinstance(cm, dict) else ""
        related: list[dict[str, Any]] | None = None
        relation_action: dict[str, Any] | None = None
        if relation_miss:
            related = _related_links(model, component=component, plan=plan)
            relation_action = _new_relation_action(
                state.lang, state.datasource or "", model, component, miss_reason)

        draft = None
        conflict: Conflict | None = None
        try:
            draft = await draft_refusal_extension(
                llm,
                config.model_for_draft("refuse", state.complexity) if config else "openai/gpt-4o",
                question,
                plan,
                model,
                lang=state.lang,
            )
        except Exception as e:
            logger.warning("Refusal drafting failed (%s): %s", state.datasource, e)
            draft = None
        if draft is not None:
            conflict = _detect_conflict(model, draft)

        entry = None
        auto_confirmed = False
        conflict_landed = False
        if draft is not None and conflict is not None and conflict.landable:
            # 可人工修正的冲突(重名/表达式/数据集引用)→ 内容照落 pending,
            # 带 conflict 注解:收件箱里看得见、确认被守卫拦住、以蓝本新建
            # 是唯一修正路径。落库失败不连坐——照旧走"只展示"通道。
            kind = draft.get("kind")
            name = str(draft.get("name") or "").strip()
            datasource = state.datasource or ""
            if kb is not None and datasource and kind in ("metric", "field") and name:
                from trove.services.semantic_layer.manage import SemanticManager
                manager = SemanticManager(kb)
                try:
                    entry = await manager.create_draft(
                        datasource, kind, "upsert", name,
                        payload=_payload_for(kind, draft),
                        note=f"refuse-conflict:{conflict.code}:{question[:120]}",
                        conflict={
                            "code": conflict.code,
                            "message": conflict.message,
                        },
                    )
                    conflict_landed = True
                except Exception as e:
                    logger.warning("Conflicted draft write failed (%s): %s",
                                   state.datasource, e)
                    entry = None
        if draft is not None and conflict is None:
            kind = draft.get("kind")
            name = str(draft.get("name") or "").strip()
            datasource = state.datasource or ""
            if kb is not None and datasource and kind in ("metric", "field") and name:
                from trove.services.semantic_layer.manage import SemanticManager
                manager = SemanticManager(kb)
                # A/B 档自动确认:确定性验证门全过 → 直接入库(跳过 pending
                # 审批),置 auto_confirmed 让图路由回 parse_date 重答。有上限
                # (auto_confirm_rounds)防同问反复自确认打转。
                # A 档 = 物理列字段(catalog 列存在);B 档 = 机械聚合指标
                # (编译 + 真实执行 shape 验证)。任一不过 → 退回 pending。
                if (
                    state.auto_confirm_rounds < AUTO_CONFIRM_MAX_ROUNDS
                    and (
                        (
                            kind == "field"
                            and await _physical_column_field(
                                connectors, datasource, model, draft) == name
                        )
                        or (
                            kind == "metric"
                            and await _mechanical_metric_ok(
                                connectors, datasource, model, draft,
                                principal=principal_from_wire(state.principal),
                            )
                        )
                    )
                ):
                    try:
                        entry = await manager.auto_apply(
                            datasource, kind, name,
                            payload=_payload_for(kind, draft),
                            note=f"refuse-auto:{question[:120]}",
                        )
                        auto_confirmed = True
                    except Exception as e:
                        logger.warning("Auto confirm failed (%s): %s",
                                       datasource, e)
                        auto_confirmed = False
                if not auto_confirmed:
                    try:
                        entry = await manager.create_draft(
                            datasource, kind, "upsert", name,
                            payload=_payload_for(kind, draft),
                            note=f"refuse:{question[:120]}",
                        )
                    except Exception as e:
                        logger.warning("Refusal draft write failed (%s): %s",
                                       state.datasource, e)
                        entry = None
            elif datasource:
                # 无 kb/数据源上下文 → 不落库,只展示草稿(文案即证据)
                entry = {"id": "", "kind": kind, "name": name,
                         "payload": _payload_for(kind, draft), "status": "pending"}

        # A 档自动确认成功 → 已入库,路由回 parse_date 立即重答(同轮闭环)。
        # 清 refusal/clarification:残留 refusal 会让 schema_linking 语义门再
        # 短路回 refuse(死循环),残留 clarification 会让 output 显示反问而非答案。
        if auto_confirmed and entry is not None:
            message = _auto_confirmed_message(state.lang, reason_detail, draft)
            return {
                "clarification_question": "",
                "refusal": None,
                "auto_confirmed": True,
                "auto_confirm_rounds": state.auto_confirm_rounds + 1,
                "question": question,
                "rewritten_question": state.question,
                "intent": "query",
                "intent_evidence": {
                    "auto_confirm": True,
                    "draft_kind": draft.get("kind"),
                    "draft_name": draft.get("name"),
                    "message": message,
                },
                # 重答不复用旧产物:交付字段清零(plan 保留,重答的
                # query_sketch 以它为上一轮上下文)。
                **_HYGIENE,
            }

        if draft is not None and conflict is not None:
            message = _uncovered_message(
                state.lang, copy_detail, draft,
                conflict=conflict, landed=conflict_landed)
        else:
            shown = draft if draft is not None else None
            message = _uncovered_message(state.lang, copy_detail, shown)
        # 管理员在对话中即可确认(无需离开会话);非管理员提示走管理端。
        # 冲突草稿例外:确认被 manage 的守卫拦住,提示=死按钮,不给。
        if state.is_admin and draft is not None and conflict is None:
            message += L(
                state.lang,
                "\n（管理员：直接回复「确认」即可在对话中采纳该草稿并立即重答。）",
                "\n(Admins: reply \"confirm\" to approve this draft and get the answer immediately.)",
            )
        actions = _draft_actions(
            state.lang, state.datasource or "",
            draft=draft, conflict=conflict, entry=entry,
        )
        if relation_action is not None:
            # 「新建关系」蓝本取代泛泛的「去语义工作台」——同一落点但预填了
            # 两端(严格更好,不并存);草稿类出口(确认 / 以蓝本新建)是另
            # 一件已发生的事,照留。
            actions = [relation_action] + [
                a for a in actions if a.get("kind") != "open_semantic"]
        # 普通拒绝:显式重置 auto_confirmed=False——A 档自确认后若同问重跑仍
        # 失败,auto_confirmed 残留 True 会把路由拉回 parse_date 造成死循环。
        refusal_out: dict[str, Any] = {
            "reason": reason,
            "question": question,
            "datasource": state.datasource or "",
            "draft": draft,
            "conflict": conflict is not None,
            "draft_entry": entry,
            "message": message,
            "next_actions": actions,
        }
        # 冲突结构化信息(``conflict`` 保持 bool 兼容老消费方,新信息走兄弟键;
        # 无冲突时不带这个键——拒绝 dict 的既有形状逐字节不变)。
        if conflict is not None:
            refusal_out["conflict_info"] = {
                "code": conflict.code,
                "message": conflict.message,
                "landed": conflict_landed,
            }
        # 结构化分因随终态拒绝一起交付:拒绝是这条链的**终态产物**(wire 上
        # 走 mcp/session 的 refusal 字段),它得能自己说清缺的是哪种声明 ——
        # 上一跳写了 compile_miss,这里重建 dict 时丢掉 = 诊断只活在日志里。
        if cm:
            refusal_out["compile_miss"] = cm
        # 结构化兄弟键(设计 §5.3):本问涉及的表/实体之间已声明的链接 ——
        # 让「缺的是哪条、已有的是哪些」变成两个可区分的事实。仅关系类
        # reason 且投影得出来时挂载;缺席 = 前端整节不渲染(非关系类、无
        # 模型、涉及集解析不出的拒绝 dict 形状逐字节不变)。
        if related is not None:
            refusal_out["related_links"] = related
        return {
            "auto_confirmed": False,
            "clarification_question": message,
            "refusal": refusal_out,
            **_HYGIENE,
        }

    return refuse
