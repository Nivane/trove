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

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlglot import ErrorLevel, exp, parse_one

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


def _auto_confirmed_message(lang: str, reason: str, draft: dict[str, Any] | None) -> str:
    """A/B 档自动确认文案:物理列字段/机械聚合指标已验证入库,正在重答。"""
    name = str(draft.get("name", "")) if draft else ""
    expr = str(draft.get("expression", "")) if draft else ""
    lines = []
    if draft:
        lines.append(f"- kind={draft.get('kind', '?')}, name={name}, expression={expr}")
        syns = draft.get("synonyms") or []
        if syns:
            lines.append("- synonyms: " + ", ".join(map(str, syns)))
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
        f"已自动补充声明（{reason}）：\n"
        + ("\n".join(lines) + "\n" if lines else "")
        + f"{gate}，已直接入库，正在重新回答你的问题。",
        f"Declaration auto-added ({reason}):\n"
        + ("\n".join(lines) + "\n" if lines else "")
        + f"{gate}, applied directly, and your question is being re-answered.",
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
                        return (
                            f"已有同表达式指标「{m.name}」"
                            f"({m.expression})——时间/枚举等过滤值应是查询条件,"
                            "请勿按值新建指标"
                        )
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
    except Exception:
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
) -> str:
    """B 档:机械聚合指标(COUNT/SUM/AVG 值无关)的确定性验证门。

    通过 → 返回 ``dataset 名``(可直接自动确认);否则返回 ""。
    验证门 = 编译通过 + 表可达 + _agg_signature 兼容 + 真实执行(shape 规则):

      1. 表达式是单个机械聚合(count/sum/avg),值无关(无 FILTER/WHERE/
         子查询把过滤值写死进指标);
      2. 单数据集、已声明、物理表存在(表可达),引用列在物理表中存在;
      3. 把草稿指标临时并入模型副本,走权威编译器 compile_detailed
         + guardrail(编译通过、投影表守卫、签名兼容)——编译器 MISS 即拒;
      4. 用物理表名重写表达式,在数据源真实执行,结果过 rules.verify
         shape 链(单行单列标量)。

    任一不过 → 退回 pending 走人工(保守,不靠 LLM 自评)。
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
        CompileMiss,
        SemanticCompiler,
        _agg_signature,
        validate_compiled_sql,
    )
    from trove.services.semantic_layer.models import SemanticMetric, SemanticModel

    # 1) 机械聚合 + 值无关
    sig = _agg_signature(expr)
    if sig is None or sig[0] not in _MECHANICAL_AGGS:
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
    except Exception:
        return ""
    tbl = next((t for t in schema.tables if t.name.lower() == physical_table.lower()), None)
    if tbl is None or (tbl.row_count_estimate or 0) > _AUTO_EXEC_MAX_ROWS:
        return ""
    phys_cols = {c.name.lower() for c in tbl.columns}
    sig_cols = {c.rsplit(".", 1)[-1].lower() for c in sig[1]}
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
        if isinstance(result, CompileMiss):
            return ""
        if validate_compiled_sql(result.sql, probe, [ds_name]):
            return ""
    except Exception:
        return ""

    # 4) 真实执行 + shape 规则(单行单列标量):执行编译器产出的权威 SQL
    #    (与生产 execute_sql 同一 SQL,忠实验证「这条指标能跑出标量」)。
    from trove.workflow.rules import verify
    try:
        res = await asyncio.wait_for(
            connectors.execute(result.sql, datasource), timeout=_AUTO_EXEC_TIMEOUT_S)
    except Exception:
        return ""
    if not res.columns or not res.rows:
        return ""
    reason, _hits = verify(
        "what is the " + sig[0] + "?",
        result.sql, res.columns, res.rows, len(res.rows), lang="en")
    if reason:
        return ""
    return ds_name


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
        auto_confirmed = False
        if draft is not None and not conflict:
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
                                connectors, datasource, model, draft)
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
            }

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
        # 普通拒绝:显式重置 auto_confirmed=False——A 档自确认后若同问重跑仍
        # 失败,auto_confirmed 残留 True 会把路由拉回 parse_date 造成死循环。
        return {
            "auto_confirmed": False,
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
