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

Node shape: ``make_refuse(llm, config, kb=None, semantic_layer=None) ->
async def refuse(state) -> dict``. Returns a partial state update.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlglot import ErrorLevel, parse_one

from trove.core.config import AgentConfig
from trove.core.i18n import L
from trove.llm.gateway import LLMGateway
from trove.services.kb.semantic_draft import draft_refusal_extension
from trove.workflow.state import WorkflowState

logger = logging.getLogger(__name__)


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


def _uncovered_message(lang: str, reason: str, draft: dict[str, Any] | None,
                       conflict: bool = False) -> str:
    if draft is None:
        return L(
            lang,
            f"当前语义模型缺少回答此问题所需的声明（{reason}）。"
            "请在管理端补充模型声明（metric/字段），然后重发此问题。",
            f"The semantic model is missing a declaration needed to answer "
            f"this question ({reason}). Add the missing metric/field in the "
            f"admin console, then re-ask.",
        )
    name = draft.get("name", "")
    expr = draft.get("expression", "")
    lines = [f"- kind={draft.get('kind', '?')}, name={name}, expression={expr}"]
    syns = draft.get("synonyms") or []
    if syns:
        lines.append("- synonyms: " + ", ".join(map(str, syns)))
    if conflict:
        return L(
            lang,
            f"当前语义模型缺少回答此问题所需的声明（{reason}）。"
            f"已尝试生成草稿但与现有模型冲突（同名定义/表达式不可解析/"
            "数据集未声明），未写入：\n"
            + "\n".join(lines) + "\n请在管理端人工补充声明。",
            f"The semantic model is missing a declaration needed to answer "
            f"this question ({reason}). A draft was attempted but conflicts "
            f"with the current model (duplicate name / unparseable expression "
            f"/ undeclared dataset) and was NOT written:\n"
            + "\n".join(lines) + "\nPlease add the declaration manually.",
        )
    return L(
        lang,
        f"当前语义模型缺少回答此问题所需的声明（{reason}）。"
        f"已生成扩展草稿：\n"
        + "\n".join(lines)
        + "\n请到管理端确认该草稿（确认后立即重答）；确认前不会生成 SQL。",
        f"The semantic model is missing a declaration needed to answer this "
        f"question ({reason}). An extension draft was generated:\n"
        + "\n".join(lines)
        + "\nConfirm the draft in the admin console (it will be re-answered "
        "immediately after confirmation); no SQL is generated until then.",
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


def _detect_conflict(model, draft: dict[str, Any]) -> str:
    """确定性冲突检测:同名定义 / 表达式不可解析 / 数据集未声明。

    返回冲突原因(空 = 无冲突,可写库)。
    """
    kind = draft.get("kind")
    expr = str(draft.get("expression") or "").strip()
    if not _expr_ok(expr):
        return "表达式不可解析"
    declared_datasets = {d.name for d in model.datasets} if model is not None else set()
    declared_metrics = {m.name for m in model.metrics} if model is not None else set()
    if kind == "metric":
        name = str(draft.get("name") or "").strip()
        if not name:
            return "指标名缺失"
        if name in declared_metrics:
            return f"指标「{name}」已声明"
        refs = draft.get("datasets") or []
        if refs and any(d not in declared_datasets for d in refs):
            bad = [d for d in refs if d not in declared_datasets]
            return f"引用了未声明的数据集 {bad}"
        return ""
    if kind == "field":
        name = str(draft.get("name") or "").strip()
        ds_name, sep, field_name = name.partition(".")
        if not sep:
            return "字段名必须是 dataset.field 形式"
        if ds_name not in declared_datasets:
            return f"数据集「{ds_name}」未声明"
        ds = next(d for d in model.datasets if d.name == ds_name)
        if any(f.name == field_name for f in ds.fields):
            return f"字段「{name}」已声明"
        return ""
    return "未知的草稿类型"


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


def make_refuse(
    llm: LLMGateway,
    config: AgentConfig,
    kb: Any | None = None,
    semantic_layer: Any | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the refuse node bound to an LLM gateway + semantic services.

    Args:
        llm: LLM gateway (used only for the uncovered-draft drafting).
        config: Agent config (model selection / language).
        kb: Optional KbService — enables writing the pending draft to
            ``semantic_drafts.yml`` for the admin confirm flow.
        semantic_layer: Optional live semantic provider — supplies the
            current model for conflict detection.
    """

    async def refuse(state: WorkflowState) -> dict[str, Any]:
        # Upstream failure — pass through
        if state.error:
            return {}

        if state.no_model:
            message = _no_model_message(state.lang)
            return {
                "clarification_question": message,
                "refusal": {
                    "reason": "no_model",
                    "question": state.question,
                    "message": message,
                },
            }

        refusal = state.refusal or {}
        if not refusal:
            return {}

        reason = str(refusal.get("reason") or "未覆盖")
        # 编译 MISS 的结构化分因(reason slug + 失败组件)→ 拼进用户可见
        # 文案,管理端知道具体缺哪个声明(metric/字段/join),而不是笼统
        # 「uncovered」。返回的 refusal["reason"] 保持原始值(上游契约/
        # 机器匹配不变),派生 detail 只进 message。
        reason_detail = reason
        cm = refusal.get("compile_miss") or {}
        if isinstance(cm, dict) and cm.get("reason"):
            detail = str(cm["reason"])
            if cm.get("component"):
                detail = f"{detail}: {cm['component']}"
            reason_detail = f"{reason} ({detail})"
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

        draft = None
        conflict = ""
        try:
            draft = await draft_refusal_extension(
                llm,
                config.model_for_node("refuse", state.complexity) if config else "openai/gpt-4o",
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
        if draft is not None and not conflict:
            kind = draft.get("kind")
            name = str(draft.get("name") or "").strip()
            datasource = state.datasource or ""
            if kb is not None and datasource and kind in ("metric", "field") and name:
                try:
                    from trove.services.semantic_layer.manage import SemanticManager
                    manager = SemanticManager(kb)
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

        if draft is not None and conflict:
            message = _uncovered_message(state.lang, reason_detail, draft, conflict=True)
        else:
            shown = draft if draft is not None else None
            message = _uncovered_message(state.lang, reason_detail, shown)
        # 管理员在对话中即可确认(无需离开会话);非管理员提示走管理端
        if state.is_admin and draft is not None and not conflict:
            message += L(
                state.lang,
                "\n（管理员：直接回复「确认」即可在对话中采纳该草稿并立即重答。）",
                "\n(Admins: reply \"confirm\" to approve this draft and get the answer immediately.)",
            )
        return {
            "clarification_question": message,
            "refusal": {
                "reason": reason,
                "question": question,
                "draft": draft,
                "conflict": bool(conflict),
                "draft_entry": entry,
                "message": message,
            },
        }

    return refuse
