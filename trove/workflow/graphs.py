"""LangGraph builders for Trove query workflows.

Composition (this module = topology; the gen chain lives in ``gen_chain.py``):
  - gen chain, three top-level nodes: gen_retrieve (recall + signals) →
    gen_assemble (budget + input assembly) → gen_generate (agentic loop +
    consensus pool). The classic generate → validate retry subgraph
    (build_gen_sql_subgraph) is the agentic loop's degradation path inside
    gen_generate (and the whole story for non-agentic / fixed graphs).
    Those factories are re-exported here for compatibility; new code should
    import ``trove.workflow.gen_chain`` directly.
  - reflection main graph: route_intent → parse_date → schema_linking →
    (semantic gate: refuse / clarify) → fast_match → query_sketch? →
    gen_retrieve → gen_assemble → gen_generate → semantics → hitl? →
    execute_sql → select → validate → masking → reflect → (conditional)
    failure → analyze_error → LLM-judged rollback (gen_sql / query_sketch /
    schema_linking, anti-loop guarded) → attribution? → insights → chart →
    conclusion → output. "gen_sql" survives only as the rollback *label*
    (analyze_targets routes it to gen_retrieve).
  - fixed main graph: same pipeline without the reflect loop
  - empty main graph: pass-through output

Graceful degradation: node failures write state.error; every downstream
node passes through untouched, and the router sends the run to output,
which formats a readable error section. The deliberate exception is masking
(validate → masking → reflect): its failure direction is deny, not degrade,
so a refusal clears rows/columns and writes [ERR:MASKING] — see
nodes/masking.py.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from trove.core.config import AgentConfig, BudgetConfig
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.llm.untrusted import screen_derived
from trove.prompts import render
from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.kb.service import KbService
from trove.services.limits import get_result_limits

# ── Compatibility re-exports (#4 orchestration split) ─────
# These names used to live in this module. Import paths stay stable on
# purpose; new code should import from the home module directly.
from trove.workflow.gen_chain import (  # noqa: F401
    COMPLEXITY_BUDGET_TOKENS,
    DEFAULT_GEN_SQL_RETRIES,
    KB_EXACT_OVERLAP,
    SCHEMA_BUDGET_TOKENS,
    _candidate_schedule,
    _make_gen_sql_node,
    _payload_sql,
    _rotate_few_shots,
    _run_candidate_subagent,
    _tier_budget,
    _word_overlap,
    build_gen_sql_subgraph,
    generation_temperature,
    make_gen_assemble,
    make_gen_generate,
    make_gen_retrieve,
)
from trove.workflow.graph_services import (  # noqa: F401
    GraphServices,
    _build_authorizer,
    _egress_redactor,
)
from trove.workflow.intent import (
    Intent,
    classify_intent,
    has_attribution_signal,
    has_followup_signal,
    has_strong_chitchat,
    has_strong_confirm,
    has_strong_correction,
    has_strong_write,
    has_weak_signal,
    last_user_question,
    parse_llm_intent,
    verify_intent,
)
from trove.workflow.nodes.analyze_error import make_analyze_error
from trove.workflow.nodes.answer import make_answer_metadata
from trove.workflow.nodes.attribution import make_attribution
from trove.workflow.nodes.chart import make_chart
from trove.workflow.nodes.clarify import make_clarify
from trove.workflow.nodes.conclusion import make_conclusion
from trove.workflow.nodes.confirm_draft import make_confirm_draft
from trove.workflow.nodes.execute_sql import make_execute_sql
from trove.workflow.nodes.fast_match import make_fast_match
from trove.workflow.nodes.hitl import make_hitl
from trove.workflow.nodes.insights import make_insights
from trove.workflow.nodes.masking import make_masking
from trove.workflow.nodes.metadata_check import make_metadata_check
from trove.workflow.nodes.output import output
from trove.workflow.nodes.parse_date import make_parse_date
from trove.workflow.nodes.query_sketch import make_query_sketch
from trove.workflow.nodes.reflect import make_reflect
from trove.workflow.nodes.refuse import make_refuse
from trove.workflow.nodes.schema_linking import make_schema_linking
from trove.workflow.nodes.select import make_select_consensus
from trove.workflow.nodes.semantics import make_semantics
from trove.workflow.nodes.terminal import (
    answer_chitchat,
    answer_correction,
    answer_reject,
)
from trove.workflow.nodes.validate import make_validate_rules
from trove.workflow.rules import (
    is_count_question,
    is_list_question,
    is_ordered_question,
    is_percent_question,
)
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

MAX_REFLECT_RETRIES = 10  # 修正轮上限（执行错误/规则/一致性/裁决共享）

WORKFLOW_NAMES = ("reflection", "fixed", "empty")

# ── Main graph builders ──────────────────────────────────


def build_graphs(
    services: GraphServices,
    checkpointer: Any = None,
    multi_candidate: bool = True,
    query_sketch: bool = True,
    clarify: bool = False,
    agentic: bool = True,
    scaling: int = 5,
) -> dict[str, CompiledStateGraph]:
    """Build and compile the reflection / fixed / empty graphs.

    Args:
        services: Service bundle bound into node closures.
        checkpointer: Optional LangGraph checkpointer (None = in-memory only).
        multi_candidate: Reflection generates alternative candidates at
            higher temperatures for consensus selection (off = single
            candidate).
        query_sketch: Reflection drafts an LLM query plan before generation.
        clarify: Ask the user instead of generating when no tables match
            (off by default — generation proceeds permissively).
        agentic: gen_sql runs a ReAct loop (model validates via the
            validate_sql tool and self-terminates); off = classic
            single-shot subgraph generation.
        scaling: Candidate pool size (N total incl. the primary). 5 = the
            historical 4 alternative subgraphs at 0.3/0.5/0.7/1.0 — the
            default is byte-identical to the pre-scaling behavior. Larger
            values (50/200) fan out de-correlated alternatives for the
            eval-side test-time-scaling A/B; the cost scales with N, so
            only opt in via the eval script's --scaling.

    Returns:
        Mapping of workflow name → compiled graph.
    """
    subgraph = build_gen_sql_subgraph(services)
    # Multi-candidate: N-1 alternative generations, one per de-correlated
    # (temperature, style-mode) config, for execution voting.
    alt_subgraphs = (
        [
            build_gen_sql_subgraph(services, temperature=t, mode=mode)
            for t, mode in _candidate_schedule(max(scaling, 1) - 1)
        ]
        if multi_candidate else None
    )

    def compile(g: StateGraph) -> CompiledStateGraph:
        return g.compile(checkpointer=checkpointer) if checkpointer else g.compile()

    return {
        "reflection": compile(_build_reflection(
            services, subgraph, alt_subgraphs=alt_subgraphs, query_sketch=query_sketch,
            clarify=clarify, agentic=agentic,
        )),
        "fixed": compile(_build_fixed(services, subgraph, clarify, agentic)),
        "empty": compile(_build_empty()),
    }


def _route_after_hitl(state: WorkflowState) -> str:
    """HITL 门后分流:被用户否决 → 直接输出(不执行);否则正常执行。"""
    if state.error or state.hitl_status == "rejected":
        return "output"
    return "execute_sql"


def _route_after_reflect(state: WorkflowState) -> Literal["analyze_error", "answer_metadata", "attribution", "insights"]:
    # Termination is guaranteed by reflect itself: it only returns RETRY
    # while retry_count < MAX_REFLECT_RETRIES (then forces OK).
    if state.error:
        return "insights"  # insights 对 error 透传,不阻断降级输出
    if state.no_sql:
        return "answer_metadata"
    if state.verdict != "RETRY":
        # 归因问题(attribution_plan 置位)→ attribution 节点多跳下钻,
        # 否则原路径 insights。attribution 节点对无计划透传,链路不中断。
        return "attribution" if state.attribution_plan else "insights"
    # RETRY goes through the diagnose-and-decide node: the LLM judges the
    # failure root cause and picks the rollback target.
    return "analyze_error"


def _make_route_after_analyze_error(targets: dict[str, str]):
    """Route by the LLM-judged rollback target (or the NO_SQL decision).

    The anti-loop escalation happens inside the analyze_error node; here
    unknown targets fall back to gen_sql so the graph always terminates.
    """

    def route_after_analyze_error(state: WorkflowState) -> str:
        if state.error:
            return "output"
        if state.no_sql:
            return "answer_metadata"
        target = state.rollback_target or "gen_sql"
        return target if target in targets else "gen_sql"

    return route_after_analyze_error


def make_route_intent(
    llm: LLMGateway | None = None,
    config: AgentConfig | None = None,
    catalog: CatalogService | None = None,
    kb: KbService | None = None,
    connectors: ConnectorRegistry | None = None,
):
    """Intent router: LLM classifies, deterministic evidence verifies.

    Five-way routing (query/metadata/write/chitchat/correction). The LLM
    always judges (a tiny call); its verdict is verified against
    evidence. Write and chitchat are answered by zero-LLM terminal nodes.
    Correction is a transform intent:
      - elliptical follow-up ("那北京呢?") → rewritten with history,
        then the full evidence chain runs again on the new question;
      - pure feedback ("不对") → substitutes the previous question from
        history and reclassifies it deterministically;
      - with nothing to act on (no history / rewrite failed) → guidance
        via answer_correction.
    Regex classification remains the fallback when the LLM is
    unavailable or its reply is unparseable.
    """

    async def _classify(
        question: str, state: WorkflowState
    ) -> tuple[Intent, dict[str, Any], dict[str, Any] | None]:
        """One classification pass over a question → (intent, evidence, llm_detail)."""
        strong = classify_intent(question)
        data_signal = any(
            f(question)
            for f in (
                is_count_question,
                is_list_question,
                is_percent_question,
                is_ordered_question,
            )
        )
        write_sig = has_strong_write(question)
        chit_sig = has_strong_chitchat(question)
        corr_sig = has_strong_correction(question)
        confirm_sig = has_strong_confirm(question)
        followup = has_followup_signal(question, state.history)
        weak_sig = has_weak_signal(question)
        attr_sig = has_attribution_signal(question)

        llm_intent: Intent | None = None
        llm_detail: dict[str, Any] | None = None
        llm_error = ""
        if llm is not None:
            # 意图分类是琐碎判别任务,固定走 fast 档(起草档配了则优先)。
            # complexity 固定 "simple" —— 与改造前直读 model_fast 逐字节一致,
            # 不被 state.complexity 分档影响(判别任务不需要复杂度分级)。
            model = config.model_for_draft("intent", "simple") if config else "openai/gpt-4o"
            intent_prompt = render("intent/system", lang=state.lang)
            start = time.monotonic()
            try:
                response = await llm.chat(
                    model=model,
                    # 推理模型 reasoning 占用预算,小预算会导致 content 为空、
                    # 意图判定永远回退 regex;统一放宽输出上限(见 gateway 默认值)
                    max_tokens=16000,
                    messages=[
                        {"role": "system", "content": intent_prompt},
                        {"role": "user", "content": question},
                    ],
                    metadata={
                        "node": "route_intent",
                        "session_id": state.session_id,
                        "run_id": state.run_id,
                        "question": question[:80],
                    },
                )
                llm_intent = parse_llm_intent(response)
                llm_detail = {
                    "model": model,
                    "elapsed_ms": int((time.monotonic() - start) * 1000),
                    "input_preview": intent_prompt[:200],
                    "output_preview": (response or "").strip()[:200],
                }
            except Exception as e:
                llm_error = str(e)[:120]
                llm_intent = None

        mentioned_table = term_hit = False
        if llm_intent == Intent.METADATA:
            # Evidence for the metadata verdict: a known table or a
            # known business term mentioned in the question.
            if catalog is not None:
                try:
                    mentioned_table = bool(
                        await catalog.search_tables(question, limit=3)
                    )
                except Exception:
                    pass
            if kb is not None and connectors is not None:
                try:
                    ds = state.datasource or connectors.default_name or ""
                    if ds:
                        await kb.ensure_synced(ds)
                        term_hit = bool(
                            await kb.search_terms(question, ds)
                        )
                except Exception:
                    pass

        if llm_intent is not None:
            intent = verify_intent(
                llm_intent,
                # strong_match 只指 metadata 强信号:classify_intent 现在也会
                # 返回 write/chitchat/correction,不能拿"任意强意图"充数
                strong_match=strong == Intent.METADATA,
                write_signal=write_sig,
                confirm_signal=confirm_sig,
                chitchat_signal=chit_sig,
                correction_signal=corr_sig,
                followup_signal=followup,
                history_present=bool(state.history),
                weak_signal=weak_sig,
                mentioned_table=mentioned_table,
                term_hit=term_hit,
                data_signal=data_signal,
                attribution_signal=attr_sig,
            )
        else:
            # LLM 不可用 → 纯正则直路由(高置信信号直接生效,与旧语义一致);
            # 省略式追问无 LLM 可重写 → correction 引导,不跑注定失败的管线
            intent = strong or (
                Intent.CORRECTION if followup and state.history else Intent.QUERY
            )
        evidence = {
            "strong_match": strong is not None,
            "data_signal": data_signal,
            "attribution_signal": attr_sig,
            "write_signal": write_sig,
            "chitchat_signal": chit_sig,
            "correction_signal": corr_sig,
            "confirm_signal": confirm_sig,
            "followup_signal": followup,
            "history_present": bool(state.history),
            "weak_signal": weak_sig,
            "llm_verdict": llm_intent.value if llm_intent else None,
            "llm_error": llm_error,
            "mentioned_table": mentioned_table,
            "term_hit": term_hit,
            "rewritten": False,
            "substituted": False,
        }
        return intent, evidence, llm_detail

    async def _rewrite_followup(question: str, state: WorkflowState) -> str:
        """LLM 用历史补全省略式追问;失败/无进展由调用方兜底。"""
        # 追问改写同为琐碎判别任务,与意图分类同款固定 fast 档(见上)。
        model = config.model_for_draft("intent_followup", "simple") if config else "openai/gpt-4o"
        prompt = render(
            "intent/followup_rewrite",
            lang=state.lang,
            history=state.history,
            question=question,
        )
        response = await llm.chat(
            model=model,
            max_tokens=16000,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": question},
            ],
            metadata={
                "node": "route_intent_rewrite",
                "session_id": state.session_id,
                "run_id": state.run_id,
                "question": question[:80],
            },
        )
        return (response or "").strip()

    async def route_intent(state: WorkflowState) -> dict[str, Any]:
        if state.error:
            return {}
        intent, evidence, llm_detail = await _classify(state.question, state)

        if intent == Intent.CORRECTION:
            if evidence["followup_signal"] and llm is not None:
                # 省略式追问 → 补全重写 → 全新问题重新走完整证据链
                try:
                    rewritten = await _rewrite_followup(state.question, state)
                except Exception:
                    rewritten = ""
                # 出生点筛查:重写 prompt 带着 history(含上一轮结果包的数据预览),
                # 产物却会被当成 ``question`` —— 白名单里"用户本人的话"那个身份 ——
                # 用进下游每一段提示词。不在这里查,历史数据就能借模型的回声洗成
                # "用户说的"。命中即**弃用**(与重写失败同一条路:回落引导话术),
                # 而不是替换成标记 —— 下游要的是一句能答的问题。
                rewritten, _hits = screen_derived(rewritten, site="followup_rewrite")
                if _hits:
                    rewritten = ""
                if rewritten and rewritten != state.question:
                    intent2, evidence2, detail2 = await _classify(rewritten, state)
                    evidence2["rewritten"] = True
                    return {
                        "question": rewritten,
                        "rewritten_question": state.question,
                        "intent": intent2.value,
                        "llm": detail2,
                        "intent_evidence": evidence2,
                    }
                # 重写失败/无进展 → 落到引导话术,不瞎路由
                return {
                    "intent": "correction",
                    "llm": llm_detail,
                    "intent_evidence": evidence,
                }
            if not evidence["followup_signal"]:
                # 纯反馈 → 替换为上一问,确定性重判后继续管线
                prev = last_user_question(state.history)
                if prev and prev != state.question:
                    strong2 = classify_intent(prev)
                    data2 = any(
                        f(prev)
                        for f in (
                            is_count_question,
                            is_list_question,
                            is_percent_question,
                            is_ordered_question,
                        )
                    )
                    evidence2 = {
                        "strong_match": strong2 == Intent.METADATA,
                        "data_signal": data2,
                        "attribution_signal": has_attribution_signal(prev),
                        "write_signal": has_strong_write(prev),
                        "chitchat_signal": has_strong_chitchat(prev),
                        "correction_signal": has_strong_correction(prev),
                        "followup_signal": False,
                        "history_present": bool(state.history),
                        "weak_signal": has_weak_signal(prev),
                        "llm_verdict": None,
                        "llm_error": "",
                        "mentioned_table": False,
                        "term_hit": False,
                        "rewritten": False,
                        "substituted": True,
                    }
                    return {
                        "question": prev,
                        "rewritten_question": state.question,
                        "intent": (strong2 or Intent.QUERY).value,
                        "llm": llm_detail,
                        "intent_evidence": evidence2,
                    }
            # 无历史可重跑 → answer_correction 引导
            return {
                "intent": "correction",
                "llm": llm_detail,
                "intent_evidence": evidence,
            }

        return {
            "intent": intent.value,
            "llm": llm_detail,
            "intent_evidence": evidence,
        }

    return route_intent


def _route_after_intent(state: WorkflowState) -> str:
    """Dispatch by classified intent (falls back to query)."""
    return state.intent


def _route_after_confirm_draft(state: WorkflowState) -> Literal["parse_date", "output"]:
    """confirm_draft 分流:已确认并替换上一问 → 继续 query 管线;否则输出引导。

    confirm_draft 成功后设置 intent="query" + question=上一问;失败/无权限
    设置 intent_answer → 走 output 终止本轮。
    """
    if state.error or state.intent_answer:
        return "output"
    return "parse_date"


def _route_after_refuse(state: WorkflowState) -> Literal["parse_date", "output"]:
    """refuse 分流:A 档自动确认成功 → 回 parse_date 重答;否则输出引导。

    refuse 自动确认时设置 auto_confirmed=True + question 替换(本问重跑);
    普通拒绝/冲突/无 draft 保持 clarification_question → 走 output 终止。
    """
    if state.error or state.auto_confirm_rounds <= 0:
        return "output"
    if state.auto_confirmed:
        return "parse_date"
    return "output"


def _add_intent_routing(g: StateGraph, services: GraphServices) -> None:
    """Shared wiring: START → route_intent → query pipeline or answer nodes."""
    g.add_node("route_intent", make_route_intent(
        services.llm, services.config, services.catalog, services.kb, services.connectors,
    ))
    # Deterministic time-expression resolution ("最近7天" → absolute range);
    # silently passes through when nothing matches or date_parser is off.
    g.add_node("parse_date", make_parse_date(services.config))
    g.add_node("answer_metadata", make_answer_metadata(
        services.catalog, kb=services.kb, connectors=services.connectors,
        llm=services.llm, config=services.config, lineage=services.lineage,
    ))
    g.add_node("metadata_check", make_metadata_check(
        services.connectors, llm=services.llm, config=services.config,
        max_retries=MAX_REFLECT_RETRIES,
    ))
    # 零 LLM 终态意图应答:write 拒绝 / 闲聊 / 纠正引导
    g.add_node("answer_reject", answer_reject)
    g.add_node("answer_chitchat", answer_chitchat)
    g.add_node("answer_correction", answer_correction)
    # 对话内草稿确认(管理员):确认后替换上一问继续 query 管线,或输出引导
    g.add_node("confirm_draft", make_confirm_draft(
        kb=services.kb, connectors=services.connectors,
    ))
    g.add_edge(START, "route_intent")
    g.add_conditional_edges(
        "route_intent",
        _route_after_intent,
        {
            "query": "parse_date",
            "attribution": "parse_date",
            "metadata": "answer_metadata",
            "write": "answer_reject",
            "chitchat": "answer_chitchat",
            "correction": "answer_correction",
            "confirm": "confirm_draft",
        },
    )
    g.add_edge("answer_reject", "output")
    g.add_edge("answer_chitchat", "output")
    g.add_edge("answer_correction", "output")
    g.add_conditional_edges(
        "confirm_draft",
        _route_after_confirm_draft,
        {"parse_date": "parse_date", "output": "output"},
    )
    g.add_edge("parse_date", "schema_linking")
    g.add_edge("answer_metadata", "metadata_check")
    # 裁决不过 → 带反馈回 answer_metadata 重答(answer_metadata 交付即清
    # 反馈、metadata_check 每轮自增 retry_count,循环按预算收敛);预算耗尽
    # 的 error 是**终态**,直落 output 交付最后一版答案。error 目标**不能**
    # 是 answer_metadata:那条路既够不着计数器也没有清反馈的一方,旧路由
    # 实测无限重生成(只有 langgraph 递归上限兜底)。
    g.add_conditional_edges(
        "metadata_check",
        _make_route_after_feedback("output", "answer_metadata", "output"),
        {"answer_metadata": "answer_metadata", "output": "output"},
    )


def _make_route_after_feedback(
    error_target: str,
    feedback_target: str,
    ok_target: str,
):
    """反馈分流工厂:error → error_target,error_feedback → feedback_target,
    否则 → ok_target。

    execute/validate/metadata 共用同一分流语义,只差目标节点——以参数化
    工厂替代三份近似重复的路由。**error_target 必须不是 feedback_target**:
    feedback 是重试信号(有消费者、有预算计数),error 是终态降级——把它
    交回重试目标,计数器与清除逻辑都不在这条路上,就是一个没有出口的循环。
    """

    def route(state: WorkflowState) -> str:
        if state.error:
            return error_target
        if state.error_feedback:
            return feedback_target
        return ok_target

    return route


def _make_route_after_fast_match(miss_target: str):
    """快径分流:命中(有模板 SQL 且无 error)→ 直接执行;miss → 正常链路。

    fast_match 刻意不是回滚目标:快径 SQL 失败由 analyze_error 诊断后走
    gen_sql/query_sketch/schema_linking 正常重生成,重生成轮 in_correction
    保证不再触发快径。
    """

    def route(state: WorkflowState) -> str:
        if state.fast_path and state.sql and not state.error:
            return "execute_sql"
        return miss_target

    return route


def _route_after_clarify_query_sketch(state: WorkflowState) -> Literal["query_sketch", "output"]:
    """Clarification needed → ask the user; otherwise proceed to planning."""
    if state.error or state.clarification_question:
        return "output"
    return "query_sketch"


def _route_semantic_gate_after_linking(state: WorkflowState) -> Literal["refuse", "clarify"]:
    """语义优先(Phase A)门:无语义模型 / 编译 MISS / 零语义匹配 → refuse 终止本轮。

    置位(no_model / refusal)即短路到 refuse 节点(不生成、不执行),否则
    走既有通道。非语义优先恒走 clarify/原有路径。
    """
    if state.error or state.no_model or state.refusal:
        return "refuse"
    return "clarify"


def _route_semantic_gate_after_linking_fast_match(
    state: WorkflowState,
) -> Literal["refuse", "fast_match"]:
    if state.error or state.no_model or state.refusal:
        return "refuse"
    return "fast_match"


def _route_semantic_gate_after_linking_gen_sql(
    state: WorkflowState,
) -> Literal["refuse", "gen_retrieve"]:
    if state.error or state.no_model or state.refusal:
        return "refuse"
    return "gen_retrieve"


def _build_guards(services: "GraphServices"):
    """组织守卫门(设计 §3a / guard 档)。``None`` = 未装配,不判。

    与 ``_build_authorizer`` 同一种「能力未接即跳过该档」的接法:没有
    SkillService(嵌入场景 / 未装技能层)→ ``None``,execute_sql 里的
    ``guards is not None`` 短路让无守卫路径逐字节不变。

    与 authz 的分工:authz 是**硬边界**(不会改的权限事实,拒绝即终态),
    guard 是**组织口径**(管理端写的方法论,重生成可以过)—— 二者判定与
    出口都不同,唯一的共同点是位置(都在执行前)。

    org 扩展总开关(``agent.extensions.org_extensions_enabled``)不在这里读:
    它由 ``SkillService.guards_for`` 每问现读(与 validator 档同一处开关),
    停用时返回空列表 —— 在装配期读一次会把这个开关钉成"重启才生效"。
    """
    from trove.services.skills.guards import GuardRunner

    if services.skills is None:
        return None
    return GuardRunner(services.skills)


def _build_profile(services: "GraphServices"):
    """执行画像的表级事实(设计 §6)。``None`` = 未装配,不查。

    与 ``_build_budget`` 同开关(``explain_row_guard``)同 connectors:预算的
    第 2 档、生成前的报价、答案里的数据新鲜度,读的是**同一份画像**。分头建
    实例 = 分头抓库 + 分头缓存,同一个问题会问三遍适配器。

    ``ProfileService`` 自带 TTL 缓存与超时跳过(§10),所以按图建一个实例即可
    —— 缓存跟着图活,不会把每次执行都变成一次全库抓取。
    """
    from trove.services.datasource.profile import ProfileService

    config = services.config or AgentConfig()
    if not getattr(config, "explain_row_guard", True):
        return None
    if services.connectors is None:
        return None
    return ProfileService(services.connectors)


def _build_terminator(services: "GraphServices"):
    """超时后的主动终止(设计 §7.3 / §10 / I4)。``None`` = 未装配,不问。

    与 ``_build_profile`` / ``_build_budget`` 同开关同 connectors —— 三者本来
    就是同一条执行轨的三段(算成本 → 按成本裁 → 超时收尾)。分头开关只会造出
    「有预算判定、却没人管那条超时查询」这种半装配态,而它看起来跟装好了一模
    一样:查询照样超时,只是没人知道它停没停。

    ``QueryTerminator`` 只是**观测点**(它读适配器已有的 ``interrupt`` 结果,
    不另发一次 kill);实例无状态,按图建一个即可。
    """
    from trove.services.sql.terminate import QueryTerminator

    config = services.config or AgentConfig()
    if not getattr(config, "explain_row_guard", True):
        return None
    if services.connectors is None:
        return None
    return QueryTerminator(services.connectors)


def _build_budget(services: "GraphServices", profile=None):
    """执行画像的成本轨(设计 §5.1 / §5.2)。``None`` = 未装配,不判也不算。

    ``explain_row_guard`` 是**唯一**的开关,沿用旧名:它一直是「要不要在执行前
    做成本判定」的意思,只是旧实现在判不出来时放行。置 false 的数据源(纯 stdio
    本地库、嵌入调用)行为与改造前逐字一致 —— 不加 LIMIT。

    阈值一律从 ``config.budget`` 取(:class:`BudgetConfig` 已兼容读取旧键),
    **不给本函数任何数值参数** —— 阈值只能有一个来源,两处各存一份迟早漂移。

    与 ``_build_authorizer`` 同一种「能力未接即跳过该档」的接法:适配器报不出
    画像时第 2 档自然跳过,不会假装查过。
    """
    from trove.services.sql.budget import BudgetService, ExecutionBudget

    config = services.config or AgentConfig()
    if not getattr(config, "explain_row_guard", True):
        return None
    connectors = services.connectors
    if connectors is None:
        return None
    # 画像实例优先用调用方传进来的(与报价、新鲜度共用一份缓存);直接调本函数
    # 的老路径自己建一个,行为与改造前一致。
    profile = profile if profile is not None else _build_profile(services)

    budget_config = getattr(config, "budget", None) or BudgetConfig()
    explain = getattr(connectors, "explain", None)
    return BudgetService(
        ExecutionBudget(
            max_rows=get_result_limits().max_rows,
            timeout_ms=int(budget_config.timeout_ms),
            soft_scan_rows=int(budget_config.soft_scan_rows),
            hard_scan_rows=int(budget_config.hard_scan_rows),
            assume_max_scan_bytes=int(budget_config.assume_max_scan_bytes),
            on_unestimable=str(budget_config.on_unestimable),
        ),
        # 直接绑 registry 的方法:它本身就是 ``(sql, datasource)`` 形状,
        # 由 BudgetService 每轮把 ``state.datasource`` 传下去。
        explain=explain,
        # parse_explain 用默认的 row_guard.estimate_max_rows(不传)。
        #
        # metadata(第 2 档,P2):画像服务自己也是 ``(datasource, tables)`` 形状,
        # 同样直接绑。有了它,sqlite / clickhouse 这类没有 EXPLAIN 解析器的方言
        # 不再**每条查询**都落保守预算 —— 一个恒为真的 degraded 字段不是信号。
        metadata=profile.estimate_rows if profile is not None else None,
    )


def _route_after_query_sketch(
    state: WorkflowState,
) -> Literal["refuse", "gen_retrieve", "analyze_error"]:
    """Query-sketch 后:编译 MISS / 无语义模型 → refuse;计划自相矛盾 →
    analyze_error(零 LLM 诊断 + 有界重规划,回滚目标 query_sketch);
    否则 gen 链入口。

    重规划信号用 ``plan_replan_pending``(本节点刚发射)而不是
    ``error_feedback``:feedback 会一直挂在 state 上直到执行成功才被清,
    按它判定会让「重规划成功」的下一跳误入 analyze_error。
    """
    if state.error or state.no_model or state.refusal:
        return "refuse"
    if state.plan_replan_pending and state.error_feedback:
        return "analyze_error"
    return "gen_retrieve"


def _route_after_clarify_gen_sql(state: WorkflowState) -> Literal["gen_retrieve", "output"]:
    """Clarification needed → ask the user; otherwise proceed to generation."""
    if state.error or state.clarification_question:
        return "output"
    return "gen_retrieve"


def _build_reflection(
    services: GraphServices,
    subgraph: CompiledStateGraph,
    subgraph_alt: CompiledStateGraph | None = None,
    alt_subgraphs: list[CompiledStateGraph] | None = None,
    query_sketch: bool = True,
    clarify: bool = False,
    agentic: bool = True,
) -> StateGraph:
    # 画像实例先造,三处共用:预算第 2 档、生成前报价、答案里的新鲜度。
    # 同一个 ProfileService = 同一份 TTL 缓存(§11 R3)。
    profile = _build_profile(services)
    budget = _build_budget(services, profile)
    g = StateGraph(WorkflowState)
    g.add_node("schema_linking", make_schema_linking(
        kb=services.kb, connectors=services.connectors,
        semantic_layer=services.semantic_layer,
    ))
    # gen 链拆三段:检索/信号采集(gen_retrieve)→ 预算裁剪与输入装配
    # (gen_assemble)→ 生成与候选共识池(gen_generate)。中间态落
    # gen_ctx / gen_sub_state 过检查点,分步可观测、可独立测试。
    g.add_node("gen_retrieve", make_gen_retrieve(services))
    g.add_node("gen_assemble", make_gen_assemble(services))
    g.add_node("gen_generate", make_gen_generate(
        services, subgraph, subgraph_alt=subgraph_alt,
        alt_subgraphs=alt_subgraphs, agentic=agentic,
        budget=budget, profiles=profile,
    ))
    g.add_edge("gen_retrieve", "gen_assemble")
    g.add_edge("gen_assemble", "gen_generate")
    g.add_node("execute_sql", make_execute_sql(services.connectors, max_retries=MAX_REFLECT_RETRIES, lineage=services.lineage, timeout_ms=int((services.config or AgentConfig()).budget.timeout_ms), budget=budget, authorizer=_build_authorizer(services), profiles=profile, terminator=_build_terminator(services), guards=_build_guards(services), semantic_layer=services.semantic_layer))
    # 语义层/配置一并传入:共识失败的反馈文本里带结果值,而那条路走的是
    # analyze_error(validate 的成功分支才到 masking,这条路不经过)。预览值
    # 在 join 前脱敏 —— 见 nodes/select.py 模块注释与 nodes/masking.py 的
    # 「为什么在 validate 之后」。
    g.add_node("select", make_select_consensus(
        services.connectors, max_retries=MAX_REFLECT_RETRIES,
        semantic_layer=services.semantic_layer,
        config=services.config or AgentConfig(),
    ))
    g.add_node("validate", make_validate_rules(
        max_retries=MAX_REFLECT_RETRIES, skills=services.skills,
        semantic_layer=services.semantic_layer))
    # 脱敏节点(设计 §5.5 G4 / I5)。位置是**判定过、LLM 之前**:放在 select
    # 里会被快径跳过(select 无候选时直接返回),放在 validate 之前会让规则链
    # 对着改写过的数据下结论。见 nodes/masking.py 的模块注释。
    g.add_node("masking", make_masking(
        semantic_layer=services.semantic_layer,
        config=services.config or AgentConfig(),
    ))
    g.add_node("reflect", make_reflect(services.llm, services.config or AgentConfig(), max_retries=MAX_REFLECT_RETRIES))
    # 说明语义 + 执行前人工确认(HITL):生成 SQL → 解释 → 确认 → 执行 → 洞察
    g.add_node("semantics", make_semantics(services.llm, services.config or AgentConfig()))
    g.add_node("hitl", make_hitl(services.config or AgentConfig()))
    g.add_node("insights", make_insights(
        services.llm, services.config or AgentConfig(), skills=services.skills))
    g.add_node("attribution", make_attribution(
        services.llm, services.config or AgentConfig(),
        connectors=services.connectors, semantic_layer=services.semantic_layer,
        skills=services.skills,
    ))
    g.add_node("chart", make_chart(
        llm=services.llm, config=services.config or AgentConfig(),
        semantic_layer=services.semantic_layer, skills=services.skills))
    g.add_node("conclusion", make_conclusion(
        services.llm, services.config or AgentConfig(), skills=services.skills))
    g.add_node("output", output)

    _add_intent_routing(g, services)
    # 确定性简单快径:模板命中 → 直接执行,跳过 query_sketch + 生成 + 裁决;
    # miss → 走正常 query_sketch/gen_sql 路径(静默降级,零状态写入)。
    g.add_node("fast_match", make_fast_match(
        kb=services.kb, connectors=services.connectors,
        config=services.config or AgentConfig(),
        semantic=services.semantic_layer,
    ))
    if clarify:
        g.add_node("clarify", make_clarify())
        g.add_node("refuse", make_refuse(services.llm, services.config or AgentConfig(), kb=services.kb, semantic_layer=services.semantic_layer, connectors=services.connectors))
        g.add_conditional_edges(
            "refuse",
            _route_after_refuse,
            {"parse_date": "parse_date", "output": "output"},
        )
        g.add_conditional_edges(
            "schema_linking",
            _route_semantic_gate_after_linking,
            {"refuse": "refuse", "clarify": "clarify"},
        )
        if query_sketch:
            g.add_node("query_sketch", make_query_sketch(services.llm, services.config or AgentConfig(), connectors=services.connectors, semantic_layer=services.semantic_layer, skills=services.skills, max_retries=MAX_REFLECT_RETRIES))
            g.add_conditional_edges(
                "clarify",
                _route_after_clarify_query_sketch,
                {"query_sketch": "fast_match", "output": "output"},
            )
            g.add_conditional_edges(
                "fast_match",
                _make_route_after_fast_match("query_sketch"),
                {"execute_sql": "execute_sql", "query_sketch": "query_sketch"},
            )
            g.add_conditional_edges(
                "query_sketch",
                _route_after_query_sketch,
                {
                    "refuse": "refuse",
                    "gen_retrieve": "gen_retrieve",
                    "analyze_error": "analyze_error",
                },
            )
        else:
            g.add_conditional_edges(
                "clarify",
                _route_after_clarify_gen_sql,
                {"gen_retrieve": "fast_match", "output": "output"},
            )
            g.add_conditional_edges(
                "fast_match",
                _make_route_after_fast_match("gen_retrieve"),
                {"execute_sql": "execute_sql", "gen_retrieve": "gen_retrieve"},
            )
    else:
        if query_sketch:
            g.add_node("query_sketch", make_query_sketch(services.llm, services.config or AgentConfig(), connectors=services.connectors, semantic_layer=services.semantic_layer, skills=services.skills, max_retries=MAX_REFLECT_RETRIES))
            g.add_node("refuse", make_refuse(services.llm, services.config or AgentConfig(), kb=services.kb, semantic_layer=services.semantic_layer, connectors=services.connectors))
            g.add_conditional_edges(
                "refuse",
                _route_after_refuse,
                {"parse_date": "parse_date", "output": "output"},
            )
            g.add_conditional_edges(
                "schema_linking",
                _route_semantic_gate_after_linking_fast_match,
                {"refuse": "refuse", "fast_match": "fast_match"},
            )
            g.add_conditional_edges(
                "fast_match",
                _make_route_after_fast_match("query_sketch"),
                {"execute_sql": "execute_sql", "query_sketch": "query_sketch"},
            )
            g.add_conditional_edges(
                "query_sketch",
                _route_after_query_sketch,
                {
                    "refuse": "refuse",
                    "gen_retrieve": "gen_retrieve",
                    "analyze_error": "analyze_error",
                },
            )
        else:
            g.add_node("refuse", make_refuse(services.llm, services.config or AgentConfig(), kb=services.kb, semantic_layer=services.semantic_layer, connectors=services.connectors))
            g.add_conditional_edges(
                "refuse",
                _route_after_refuse,
                {"parse_date": "parse_date", "output": "output"},
            )
            g.add_conditional_edges(
                "schema_linking",
                _route_semantic_gate_after_linking_fast_match,
                {"refuse": "refuse", "fast_match": "fast_match"},
            )
            g.add_conditional_edges(
                "fast_match",
                _make_route_after_fast_match("gen_retrieve"),
                {"execute_sql": "execute_sql", "gen_retrieve": "gen_retrieve"},
            )
    # 生成路径:gen_sql → semantics(说明语义) → hitl(人工确认) → execute。
    # hitl 被用户否决 → 直接 output(不执行);批准/未开启 → execute_sql。
    # 确定性快径(fast_match)仍直达 execute_sql,跳过生成级别的确认。
    g.add_edge("gen_generate", "semantics")
    g.add_edge("semantics", "hitl")
    g.add_conditional_edges(
        "hitl",
        _route_after_hitl,
        {"execute_sql": "execute_sql", "output": "output"},
    )
    g.add_edge("execute_sql", "select")
    g.add_edge("select", "validate")
    # Rollback ladder mirrors the graph topology: without the query_sketch node,
    # the judge can never escalate to it.
    rollback_ladder = (
        ("gen_sql", "query_sketch", "schema_linking") if query_sketch
        else ("gen_sql", "schema_linking")
    )
    g.add_node("analyze_error", make_analyze_error(
        services.llm, services.config or AgentConfig(), rollback_ladder=rollback_ladder,
        skills=services.skills,
    ))
    analyze_targets = {
        # rollback 语义目标名保持不变("gen_sql"),实际路由到 gen 链入口
        "gen_sql": "gen_retrieve",
        "answer_metadata": "answer_metadata",
        "output": "output",
    }
    if query_sketch:
        analyze_targets["query_sketch"] = "query_sketch"
    analyze_targets["schema_linking"] = "schema_linking"
    g.add_conditional_edges(
        "analyze_error",
        _make_route_after_analyze_error(analyze_targets),
        analyze_targets,
    )
    # 执行/规则失败 → analyze_error 诊断 → 重生成。execute_sql 自守预算
    # (耗尽走 state.error、成功清 feedback),循环必然终止:error_feedback
    # 置位 ⇒ 诊断后重生成;下一轮要么清掉要么降级。
    # 成功分支先进 masking 再进 reflect:脱敏是**无条件**的后置步,不是某个
    # 分支的待遇(与 P2 修的 G3 同一条纪律:后置步只在一条路上发生 = 洞)。
    g.add_conditional_edges(
        "validate",
        _make_route_after_feedback("output", "analyze_error", "masking"),
        {"analyze_error": "analyze_error", "masking": "masking", "output": "output"},
    )
    g.add_edge("masking", "reflect")
    g.add_conditional_edges(
        "reflect",
        _route_after_reflect,
        {"analyze_error": "analyze_error", "answer_metadata": "answer_metadata", "attribution": "attribution", "insights": "insights"},
    )
    g.add_edge("attribution", "insights")
    g.add_edge("insights", "chart")
    g.add_edge("chart", "conclusion")
    g.add_edge("conclusion", "output")
    g.add_edge("output", END)
    return g


def _build_fixed(
    services: GraphServices,
    subgraph: CompiledStateGraph,
    clarify: bool = False,
    agentic: bool = True,
) -> StateGraph:
    # 与 reflection 同一份画像实例(见 _build_reflection 注释)。
    profile = _build_profile(services)
    budget = _build_budget(services, profile)
    g = StateGraph(WorkflowState)
    g.add_node("schema_linking", make_schema_linking(
        kb=services.kb, connectors=services.connectors,
        semantic_layer=services.semantic_layer,
    ))
    # gen 链拆三段:检索/装配/生成(与 reflection 同构;fixed 无裁决循环)。
    g.add_node("gen_retrieve", make_gen_retrieve(services))
    g.add_node("gen_assemble", make_gen_assemble(services))
    g.add_node("gen_generate", make_gen_generate(
        services, subgraph, agentic=agentic, budget=budget, profiles=profile))
    g.add_edge("gen_retrieve", "gen_assemble")
    g.add_edge("gen_assemble", "gen_generate")
    g.add_node("execute_sql", make_execute_sql(services.connectors, max_retries=MAX_REFLECT_RETRIES, lineage=services.lineage, timeout_ms=int((services.config or AgentConfig()).budget.timeout_ms), budget=budget, authorizer=_build_authorizer(services), profiles=profile, terminator=_build_terminator(services), guards=_build_guards(services), semantic_layer=services.semantic_layer))
    g.add_node("validate", make_validate_rules(
        max_retries=MAX_REFLECT_RETRIES, skills=services.skills,
        semantic_layer=services.semantic_layer))
    # 说明语义 + 执行前人工确认(HITL) + 执行后洞察
    g.add_node("semantics", make_semantics(services.llm, services.config or AgentConfig()))
    g.add_node("hitl", make_hitl(services.config or AgentConfig()))
    g.add_node("insights", make_insights(
        services.llm, services.config or AgentConfig(), skills=services.skills))
    g.add_node("attribution", make_attribution(
        services.llm, services.config or AgentConfig(),
        connectors=services.connectors, semantic_layer=services.semantic_layer,
        skills=services.skills,
    ))
    g.add_node("chart", make_chart(
        llm=services.llm, config=services.config or AgentConfig(),
        semantic_layer=services.semantic_layer, skills=services.skills))
    g.add_node("conclusion", make_conclusion(
        services.llm, services.config or AgentConfig(), skills=services.skills))
    g.add_node("output", output)

    _add_intent_routing(g, services)
    g.add_node("refuse", make_refuse(services.llm, services.config or AgentConfig(), kb=services.kb, semantic_layer=services.semantic_layer, connectors=services.connectors))
    g.add_conditional_edges(
        "refuse",
        _route_after_refuse,
        {"parse_date": "parse_date", "output": "output"},
    )
    if clarify:
        g.add_node("clarify", make_clarify())
        g.add_conditional_edges(
            "schema_linking",
            _route_semantic_gate_after_linking,
            {"refuse": "refuse", "clarify": "clarify"},
        )
        g.add_conditional_edges(
            "clarify",
            _route_after_clarify_gen_sql,
            {"gen_retrieve": "gen_retrieve", "output": "output"},
        )
    else:
        g.add_conditional_edges(
            "schema_linking",
            _route_semantic_gate_after_linking_gen_sql,
            {"refuse": "refuse", "gen_retrieve": "gen_retrieve"},
        )
    g.add_edge("gen_generate", "semantics")
    g.add_edge("semantics", "hitl")
    g.add_conditional_edges(
        "hitl",
        _route_after_hitl,
        {"execute_sql": "execute_sql", "output": "output"},
    )
    g.add_edge("execute_sql", "validate")
    # 与 reflection 同构:成功分支先脱敏再交下游(此处下游是 attribution)。
    g.add_node("masking", make_masking(
        semantic_layer=services.semantic_layer,
        config=services.config or AgentConfig(),
    ))
    g.add_conditional_edges(
        "validate",
        _make_route_after_feedback("output", "gen_retrieve", "masking"),
        {"gen_retrieve": "gen_retrieve", "masking": "masking", "output": "output"},
    )
    g.add_edge("masking", "attribution")
    g.add_edge("attribution", "insights")
    g.add_edge("insights", "chart")
    g.add_edge("chart", "conclusion")
    g.add_edge("conclusion", "output")
    g.add_edge("output", END)
    return g


def _build_empty() -> StateGraph:
    g = StateGraph(WorkflowState)
    g.add_node("output", output)
    g.add_edge(START, "output")
    g.add_edge("output", END)
    return g
