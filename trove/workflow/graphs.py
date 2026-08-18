"""LangGraph builders for Trove query workflows.

Composition:
  - gen_sql subgraph: generate → validate retry loop (max_retries attempts)
  - reflection main graph: schema_linking → gen_sql[subgraph] → execute_sql
    → reflect → (conditional) failure → analyze_error → LLM-judged rollback
    (gen_sql / planner / schema_linking, anti-loop guarded) or output
  - fixed main graph: same pipeline without the reflect loop
  - empty main graph: pass-through output

Graceful degradation: node failures write state.error; every downstream
node passes through untouched, and the router sends the run to output,
which formats a readable error section.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.kb.service import KbService
from trove.llm.agent_loop import run_agent_loop
from trove.workflow.context_budget import assemble_blocks
from trove.workflow.state import GenSQLState, WorkflowState

from trove.workflow.nodes.schema_linking import make_schema_linking
from trove.workflow.nodes.parse_date import make_parse_date
from trove.workflow.nodes.clarify import make_clarify
from trove.workflow.nodes.planner import make_planner
from trove.workflow.nodes.gen_sql import (
    build_sql_prompt,
    make_generate,
    make_validate,
    render_lessons,
    render_shots,
    render_terms,
)
from trove.prompts import render
from trove.workflow.nodes.execute_sql import make_execute_sql
from trove.workflow.nodes.select import make_select_consensus
from trove.workflow.nodes.validate import make_validate_rules
from trove.workflow.nodes.reflect import make_reflect
from trove.workflow.nodes.output import output
from trove.workflow.nodes.answer import make_answer_metadata
from trove.workflow.nodes.metadata_check import make_metadata_check
from trove.workflow.nodes.analyze_error import make_analyze_error, render_reasoning_context
from trove.core.i18n import L
from trove.workflow.intent import (
    Intent,
    classify_intent,
    parse_llm_intent,
    verify_intent,
)
from trove.workflow.rules import (
    is_count_question,
    is_list_question,
    is_ordered_question,
    is_percent_question,
)

logger = get_logger(__name__)

MAX_REFLECT_RETRIES = 10  # 修正轮上限（执行错误/规则/一致性/裁决共享）
CONTEXT_BUDGET_TOKENS = 2500  # gen prompt 可选块（示例/术语/教训/计划/历史）的预算


def generation_temperature(retry_count: int) -> float:
    """修正轮升温：温度 0 的确定性生成会让重试产出相同 SQL。"""
    return min(0.3, retry_count * 0.1)


def _lesson_table_ok(lesson: dict, matched: list[str], all_tables: list[str]) -> bool:
    """Hint Bank 经验按表锚过滤：提到未匹配表的教训与当前问题无关。"""
    if not matched or not all_tables:
        return True
    text = " ".join([
        str(lesson.get("pattern", "")),
        str(lesson.get("note", "")),
        str(lesson.get("sql_snippet", "")),
    ])
    mentioned = [t for t in all_tables if t and t in text]
    return not mentioned or any(t in matched for t in mentioned)


def _word_overlap(q1: str, q2: str) -> float:
    """两个问题的词重叠率(min 侧归一):KB 示例精确命中的判定依据。"""
    w1 = set(re.findall(r"[a-z0-9_]+", (q1 or "").lower()))
    w2 = set(re.findall(r"[a-z0-9_]+", (q2 or "").lower()))
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / min(len(w1), len(w2))


KB_EXACT_OVERLAP = 0.95  # KB 示例与问题几乎逐词一致 → 直接用示例 SQL
DEFAULT_GEN_SQL_RETRIES = 3

WORKFLOW_NAMES = ("reflection", "fixed", "empty")


@dataclass
class GraphServices:
    """Service bundle bound into node closures at graph build time."""

    llm: LLMGateway
    catalog: CatalogService | None = None
    connectors: ConnectorRegistry | None = None
    config: AgentConfig | None = None
    kb: KbService | None = None  # optional knowledge base enhancement


def _rotate_few_shots(sub_state: GenSQLState, offset: int) -> GenSQLState:
    """按候选索引轮换 few-shot 首条目(候选多样性:示例锚点各异)。

    各候选温度固定之外,轮换参考示例的出场顺序——模型通常以第一个
    示例为模式锚点,轮换 = 不同候选采纳不同示例的口径/风格,投票才有
    真正的分歧可统计(所有候选共享同一组示例会趋同)。少于此两条示例
    或未注入(few_shots 为 None)时原样返回。
    """
    shots = sub_state.few_shots or []
    if len(shots) < 2:
        return sub_state
    off = offset % len(shots)
    return sub_state.model_copy(update={
        "few_shots": shots[off:] + shots[:off],
    })


# ── gen_sql subgraph ─────────────────────────────────────


def build_gen_sql_subgraph(
    services: GraphServices,
    max_retries: int = DEFAULT_GEN_SQL_RETRIES,
    temperature: float = 0.0,
) -> CompiledStateGraph:
    """Build the gen_sql subgraph: generate → validate retry loop.

    Args:
        temperature: Sampling temperature (alternative candidates use
            a higher value for diversity).
    """
    config = services.config or AgentConfig()

    def route_after_validate(state: GenSQLState) -> str:
        if state.error or not state.validation_errors:
            return END
        return "generate"

    g = StateGraph(GenSQLState)
    g.add_node("generate", make_generate(services.llm, config, temperature=temperature))
    g.add_node("validate", make_validate(max_retries=max_retries))
    g.add_edge(START, "generate")
    g.add_edge("generate", "validate")
    g.add_conditional_edges(
        "validate",
        route_after_validate,
        {"generate": "generate", END: END},
    )
    return g.compile()


# ── Main graph nodes ─────────────────────────────────────


def _make_gen_sql_node(
    services: GraphServices,
    subgraph: CompiledStateGraph,
    subgraph_alt: CompiledStateGraph | None = None,
    alt_subgraphs: list[CompiledStateGraph] | None = None,
    agentic: bool = True,
):
    """gen_sql node — agentic by default: a ReAct loop where the model
    validates SQL via the validate_sql tool and ends when IT judges the
    SQL ready (model-driven termination). Content-only first response
    behaves exactly like the classic single-shot generation.

    Multi-candidate mode (subgraph_alt / alt_subgraphs given): extra
    candidates are generated at higher temperatures and stored in
    state.candidates for the consensus select node's execution voting.
    alt_subgraphs (one per temperature) takes precedence over
    subgraph_alt; subgraph_alt alone yields a single candidate.
    """

    async def gen_sql(state: WorkflowState) -> dict[str, Any]:
        if state.error:
            return {}

        # Detect dialect from the active datasource adapter
        dialect = state.dialect
        if services.connectors:
            try:
                adapter = await services.connectors.get()
                dialect = adapter.dialect()
            except Exception:
                pass

        # Knowledge base: reference examples + terminology for the prompt.
        # Scoped to the active datasource; without a datasource context the
        # KB is not consulted.
        datasource = ""
        if services.connectors is not None:
            datasource = services.connectors.default_name or ""

        few_shots: list[dict[str, Any]] = []
        term_notes: list[dict[str, Any]] = []
        example_hits = []
        lessons: list[dict[str, Any]] = []
        rules: list[str] = []
        if services.kb is not None and datasource:
            await services.kb.ensure_synced(default_datasource=datasource)
            # 证据层：以 schema linking 的 matched_tables 为锚做确定性过滤
            matched = list(state.matched_tables or [])
            all_table_names: list[str] = []
            if services.catalog is not None and matched:
                try:
                    all_table_names = [
                        t["name"] for t in await services.catalog.list_tables(datasource)
                    ]
                except Exception:
                    all_table_names = []
            # limit=5:检索注入 5 个示例(实测 3 个时注入列覆盖仅 28%,
            # 5 个 → 42%,token 成本 ~+60,预算(2500)完全装得下)
            example_hits = await services.kb.search_examples(
                state.question, datasource, limit=5,
                tables=matched or None, all_tables=all_table_names or None,
                # 多表锚定:每表分组 top 再合并,避免单表模板挤占
                per_table=bool(matched),
            )
            rules = await services.kb.list_rules(datasource)
            all_lessons = await services.kb.list_lessons(datasource)
            haystack = (state.question + " " + state.error_feedback).lower()
            lessons = [
                l for l in all_lessons
                if l.get("pattern", "").lower() in haystack
                and _lesson_table_ok(l, matched, all_table_names)
            ][:3]
            few_shots = [
                {"question": h.question, "sql": h.sql, "template": h.template}
                for h in example_hits
            ]
            term_notes = [
                {"term": h.term, "mapping": h.mapping, "definition": h.definition}
                for h in await services.kb.search_terms(
                    state.question, datasource,
                    tables=matched or None, all_tables=all_table_names or None,
                )
            ]

        # KB 精确命中:示例问题与当前问题几乎逐词一致 → 直接采用示例 SQL。
        # KB 保存的是该数据源的标准写法;对已收录的问题让模型"再解释一遍"
        # 只会产出歧义变体(实测:disp vs client 口径摇摆 5 轮)。SQL 仍会
        # 走 execute → validate → reflect,确定性规则与裁决不受影响。
        kb_exact_match: dict[str, Any] | None = None
        if example_hits:
            for h in example_hits:
                if _word_overlap(state.question, h.question) >= KB_EXACT_OVERLAP and h.sql:
                    kb_exact_match = {"question": h.question, "sql": h.sql}
                    break

        # Context budget: optional blocks filled by priority, usage
        # reported for observability (what the model actually saw).
        optional_blocks = {
            "few_shots": render_shots(few_shots),
            "rules": "\n".join(rules),
            "term_notes": render_terms(term_notes),
            "lessons": render_lessons(lessons),
            "plan": state.plan,
            "history": state.history,
        }
        included, context_usage = assemble_blocks(
            optional_blocks,
            {"few_shots": 1, "rules": 2, "term_notes": 3, "lessons": 4, "plan": 5, "history": 6},
            CONTEXT_BUDGET_TOKENS,
        )

        sub_state = GenSQLState(
            question=state.question,
            session_id=state.session_id,
            run_id=state.run_id,
            schema_context=state.schema_context,
            dialect=dialect,
            lang=state.lang,
            time_context=state.time_context,
            reflect_reason=state.reason,
            error_feedback=state.error_feedback,
            error_analysis=state.error_analysis,
            reasoning_context=render_reasoning_context(
                state.reasoning_history, ("gen_sql",),
            ),
            rejected_hypotheses=state.rejected_hypotheses,
            sql_versions=state.sql_versions,
            # 缺口3:修复模式(analyze_error 判定)传入 sub_state,重生成
            # prompt 显式区分 fixer(实现级定点修)vs revisor(语义重写)
            fix_mode=state.fix_mode,
            # Fixer 模式:打回轮(state.sql 是上一版失败 SQL)注入全文,
            # 指示模型局部修复而非整体重写
            previous_sql=(
                state.sql if (state.error_feedback or state.error_analysis or state.reason) else ""
            ),
            history=state.history if "history" in included else "",
            plan=state.plan if "plan" in included else "",
            evidence=state.evidence,
            few_shots=few_shots if "few_shots" in included else None,
            term_notes=term_notes if "term_notes" in included else None,
            lessons=lessons if "lessons" in included else None,
            rules=rules if "rules" in included else None,
        )
        update: dict[str, Any] = {
            "dialect": dialect,
            "candidates": [],
            "context_usage": context_usage,
        }

        if kb_exact_match is not None:
            # KB 精确命中:直接用标准 SQL,跳过模型生成(避免歧义变体)
            update["sql"] = kb_exact_match["sql"]
            update["kb_exact_match"] = True
            logger.info(
                "KB exact match used: %r", kb_exact_match["question"][:80],
            )
        elif agentic:
            from trove.workflow.nodes.gen_sql import (
                extract_sql, make_sql_tools,
            )

            # 工具定义与 handler 统一由工厂提供(validate_sql 始终可用,
            # probe_query/check_result 依赖 connectors);check_hits 收集
            # check_result 的规则命中,循环结束后随 update 带出(归因)
            tools, tool_handlers, check_hits = make_sql_tools(
                services.connectors, sub_state.question, sub_state.lang, dialect,
            )

            prompt = _build_gen_prompt(sub_state)
            model = (services.config.target if services.config else "") or "openai/gpt-4o"
            result = None
            try:
                result = await run_agent_loop(
                services.llm, model,
                system=render(
                    "gen_sql/system",
                    lang=sub_state.lang,
                    has_probe=services.connectors is not None,
                ),
                user=prompt,
                tools=tools,
                tool_handlers=tool_handlers,
                max_rounds=6,
                metadata={"node": "gen_sql", "session_id": state.session_id, "run_id": state.run_id},
                temperature=generation_temperature(state.retry_count),
            )
            except Exception as e:
                logger.warning("Agentic gen_sql failed (%s); falling back to classic", e)
                result = None

            async def _classic_fallback() -> None:
                """经典单发子图生成(异常或 agent loop 空手而归时兜底)。"""
                out = await subgraph.ainvoke(sub_state)
                if out["sql"]:
                    update["sql"] = out["sql"]
                if out.get("attempts"):
                    update["attempts"] = out["attempts"]
                if out["error"]:
                    update["error"] = out["error"]

            if result is not None:
                sql = extract_sql(result["content"])
                if not sql:
                    # 模型可能只在工具里给出 SQL，最终 content 无 SQL 回显
                    # (validate/probe/check 过都算——捞最近一次工具里的 SQL)
                    for entry in reversed(result.get("tool_history") or []):
                        if entry["name"] in ("validate_sql", "probe_query", "check_result") and entry["arguments"].get("sql"):
                            sql = entry["arguments"]["sql"]
                            break
                update["attempts"] = result["rounds"]
                # check_result 规则命中随状态带出(与既有 hits 合并,不覆盖
                # validate 层已记录的拦截)
                if check_hits:
                    update["validation_hits"] = list(state.validation_hits) + check_hits
                trail = " ".join(
                    p for p in (result.get("reasoning", ""), result.get("transcript", "")) if p
                )
                if trail:
                    update["reasoning_history"] = [{"node": "gen_sql", "text": trail[:800]}]
                if sql:
                    update["sql"] = sql
                elif result["guard_hit"]:
                    update["error"] = "SQL generation loop hit the round guard without producing SQL"
                else:
                    # loop 正常结束但没产出 SQL(如最后一轮只有 reasoning、
                    # content 为空且未调工具)——兜底到经典生成,不静默空转
                    logger.warning(
                        "Agentic gen_sql produced no SQL (%d rounds); falling back to classic",
                        result["rounds"],
                    )
                    await _classic_fallback()
            else:
                await _classic_fallback()
        else:
            out = await subgraph.ainvoke(sub_state)
            if out["sql"]:
                update["sql"] = out["sql"]
            if out.get("attempts"):
                update["attempts"] = out["attempts"]  # 子图内校验重试次数
            if out.get("llm"):
                update["llm"] = out["llm"]
            if out["error"]:
                update["error"] = out["error"]
        # Multi-candidate: extra generations at higher temperatures;
        # failures fall back to the single-candidate path silently.
        # KB 精确命中时跳过备选生成——备选的"另一种解释"只会引发
        # 无谓的一致性拉锯。
        # 每个温度子图各跑一次(总候选 = 1 primary + N alt),重复文本
        # 不重复投票(与 primary 相同或彼此相同则跳过,继续下个温度)。
        if (
            (subgraph_alt is not None or alt_subgraphs)
            and update.get("sql") and not update.get("error")
            and not update.get("kb_exact_match")
        ):
            alt_graphs = alt_subgraphs if alt_subgraphs else [subgraph_alt]
            # 候选池跨轮累积(重试=加票):旧候选保留,新候选加入后一起投票。
            # 平局轮打回 gen_sql 时 state.candidates 是上一轮的池——同组票数
            # 跨轮相加,正确解释的证据可以跨轮聚合,而不是每轮推倒重来。
            seen = {" ".join(update["sql"].split()).lower()}
            seen.update(" ".join(c.split()).lower() for c in state.candidates)
            candidates = list(state.candidates)
            # 并行生成:各温度子图互不依赖,asyncio.gather 把候选生成时间
            # 从 N×串行压到单次耗时(容错:异常子图静默跳过)。
            # P2-7:每个备选按索引轮换 few-shot 首条目——不同候选锚定
            # 不同参考示例,避免共享同一组示例导致候选趋同。
            outs = await asyncio.gather(
                *(
                    g.ainvoke(_rotate_few_shots(
                        sub_state.model_copy(deep=True), idx + 1))
                    for idx, g in enumerate(alt_graphs)
                ),
                return_exceptions=True,
            )
            for out_alt in outs:
                if isinstance(out_alt, BaseException):
                    continue
                alt_sql = out_alt.get("sql", "")
                if not alt_sql or out_alt.get("error"):
                    continue
                key = " ".join(alt_sql.split()).lower()
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(alt_sql)
            if candidates:
                update["candidates"] = candidates
        if example_hits:
            update["kb_hits"] = [
                {"kind": "example", "question": h.question, "sql": h.sql, "tags": h.tags}
                for h in example_hits
            ]
        return update

    return gen_sql


# ── Main graph builders ──────────────────────────────────


def build_graphs(
    services: GraphServices,
    checkpointer: Any = None,
    multi_candidate: bool = True,
    planner: bool = True,
    clarify: bool = False,
    agentic: bool = True,
) -> dict[str, CompiledStateGraph]:
    """Build and compile the reflection / fixed / empty graphs.

    Args:
        services: Service bundle bound into node closures.
        checkpointer: Optional LangGraph checkpointer (None = in-memory only).
        multi_candidate: Reflection generates a second candidate at higher
            temperature for consensus selection (off = single candidate).
        planner: Reflection drafts an LLM query plan before generation.
        clarify: Ask the user instead of generating when no tables match
            (off by default — generation proceeds permissively).
        agentic: gen_sql runs a ReAct loop (model validates via the
            validate_sql tool and self-terminates); off = classic
            single-shot subgraph generation.

    Returns:
        Mapping of workflow name → compiled graph.
    """
    subgraph = build_gen_sql_subgraph(services)
    # Multi-candidate: 4 alternative generations, one per temperature
    # (N = 5 candidates total incl. the primary), for execution voting.
    alt_subgraphs = (
        [build_gen_sql_subgraph(services, temperature=t) for t in (0.3, 0.5, 0.7, 1.0)]
        if multi_candidate else None
    )

    def compile(g: StateGraph) -> CompiledStateGraph:
        return g.compile(checkpointer=checkpointer) if checkpointer else g.compile()

    return {
        "reflection": compile(_build_reflection(
            services, subgraph, alt_subgraphs=alt_subgraphs, planner=planner,
            clarify=clarify, agentic=agentic,
        )),
        "fixed": compile(_build_fixed(services, subgraph, clarify, agentic)),
        "empty": compile(_build_empty()),
    }


def _route_after_reflect(state: WorkflowState) -> Literal["analyze_error", "answer_metadata", "output"]:
    # Termination is guaranteed by reflect itself: it only returns RETRY
    # while retry_count < MAX_REFLECT_RETRIES (then forces OK).
    if state.error:
        return "output"
    if state.no_sql:
        return "answer_metadata"
    if state.verdict != "RETRY":
        return "output"
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

    The LLM always judges (a tiny two-way call); its verdict is then
    verified against evidence: a METADATA verdict needs substance (strong
    signal / known table / known term), a QUERY verdict is overridden by
    a strong metadata signal without a data-question signal. Regex
    classification remains as the fallback when the LLM is unavailable
    or its reply is unparseable.
    """

    async def route_intent(state: WorkflowState) -> dict[str, Any]:
        if state.error:
            return {}
        strong = classify_intent(state.question)
        data_signal = any(
            f(state.question)
            for f in (
                is_count_question,
                is_list_question,
                is_percent_question,
                is_ordered_question,
            )
        )
        llm_intent: Intent | None = None
        llm_detail: dict[str, Any] | None = None
        llm_error = ""
        mentioned_table = term_hit = False
        if llm is not None:
            model = (config.target if config else "") or "openai/gpt-4o"
            intent_prompt = render("intent/system", lang=state.lang)
            start = time.monotonic()
            try:
                response = await llm.chat(
                    model=model,
                    # 推理模型 reasoning 占用预算,16 会导致 content 为空、
                    # 意图判定永远回退 regex;100 给 reasoning+单词留出空间
                    max_tokens=100,
                    messages=[
                        {"role": "system", "content": intent_prompt},
                        {"role": "user", "content": state.question},
                    ],
                    metadata={
                        "node": "route_intent",
                        "session_id": state.session_id,
                        "run_id": state.run_id,
                        "question": state.question[:80],
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

        if llm_intent is not None:
            if llm_intent == Intent.METADATA:
                # Evidence for the metadata verdict: a known table or a
                # known business term mentioned in the question.
                if catalog is not None:
                    try:
                        mentioned_table = bool(
                            await catalog.search_tables(state.question, limit=3)
                        )
                    except Exception:
                        pass
                if kb is not None and connectors is not None:
                    try:
                        ds = connectors.default_name or ""
                        if ds:
                            await kb.ensure_synced(ds)
                            term_hit = bool(
                                await kb.search_terms(state.question, ds)
                            )
                    except Exception:
                        pass
            intent = verify_intent(
                llm_intent,
                strong_match=strong is not None,
                mentioned_table=mentioned_table,
                term_hit=term_hit,
                data_signal=data_signal,
            )
        else:
            intent = strong if strong is not None else Intent.QUERY
        return {
            "intent": intent.value,
            "llm": llm_detail,
            "intent_evidence": {
                "strong_match": strong is not None,
                "data_signal": data_signal,
                "llm_verdict": llm_intent.value if llm_intent else None,
                "llm_error": llm_error,
                "mentioned_table": mentioned_table,
                "term_hit": term_hit,
            },
        }

    return route_intent


def _route_after_intent(state: WorkflowState) -> str:
    """Dispatch by classified intent (falls back to query)."""
    return state.intent


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
        llm=services.llm, config=services.config,
    ))
    g.add_node("metadata_check", make_metadata_check(
        services.connectors, llm=services.llm, config=services.config,
        max_retries=MAX_REFLECT_RETRIES,
    ))
    g.add_edge(START, "route_intent")
    g.add_conditional_edges(
        "route_intent",
        _route_after_intent,
        {
            "query": "parse_date",
            "metadata": "answer_metadata",
        },
    )
    g.add_edge("parse_date", "schema_linking")
    g.add_edge("answer_metadata", "metadata_check")
    g.add_conditional_edges(
        "metadata_check",
        _route_after_metadata_check,
        {"answer_metadata": "answer_metadata", "output": "output"},
    )


def _build_gen_prompt(sub_state: GenSQLState) -> str:
    """Assemble the generation user prompt from the prepared sub-state."""
    return build_sql_prompt(
        question=sub_state.question,
        schema_context=sub_state.schema_context,
        dialect=sub_state.dialect,
        reflect_reason=sub_state.reflect_reason,
        error_feedback=sub_state.error_feedback,
        error_analysis=sub_state.error_analysis,
        reasoning_context=sub_state.reasoning_context,
        rejected_hypotheses=sub_state.rejected_hypotheses or None,
        previous_sql=sub_state.previous_sql,
        sql_versions=sub_state.sql_versions or None,
        fix_mode=sub_state.fix_mode,
        history=sub_state.history,
        plan=sub_state.plan,
        evidence=sub_state.evidence,
        time_context=sub_state.time_context,
        rules=sub_state.rules or None,
        lessons=sub_state.lessons or None,
        few_shots=sub_state.few_shots or None,
        term_notes=sub_state.term_notes or None,
    )


def _route_after_metadata_check(state: WorkflowState) -> Literal["answer_metadata", "output"]:
    """Judge/rule failure feeds back to the metadata answer; otherwise output."""
    if state.error or state.error_feedback:
        return "answer_metadata"
    return "output"


def _route_after_clarify_planner(state: WorkflowState) -> Literal["planner", "output"]:
    """Clarification needed → ask the user; otherwise proceed to planning."""
    if state.error or state.clarification_question:
        return "output"
    return "planner"


def _route_after_clarify_gen_sql(state: WorkflowState) -> Literal["gen_sql", "output"]:
    """Clarification needed → ask the user; otherwise proceed to generation."""
    if state.error or state.clarification_question:
        return "output"
    return "gen_sql"


def _route_after_execute(state: WorkflowState) -> Literal["analyze_error", "reflect", "output"]:
    """Execution failure → error diagnosis → regeneration.

    execute_sql enforces the budget itself (degrades via state.error when
    exhausted, clears feedback on success), so the loop always terminates:
    error_feedback set ⇒ analyze then regenerate; next failure either
    clears or degrades.
    """
    if state.error:
        return "output"
    if state.error_feedback:
        return "analyze_error"
    return "reflect"


def _build_reflection(
    services: GraphServices,
    subgraph: CompiledStateGraph,
    subgraph_alt: CompiledStateGraph | None = None,
    alt_subgraphs: list[CompiledStateGraph] | None = None,
    planner: bool = True,
    clarify: bool = False,
    agentic: bool = True,
) -> StateGraph:
    g = StateGraph(WorkflowState)
    g.add_node("schema_linking", make_schema_linking(
        services.catalog, kb=services.kb, connectors=services.connectors,
        fallback_all=not clarify,
        llm=services.llm, config=services.config or AgentConfig(),
    ))
    g.add_node("gen_sql", _make_gen_sql_node(
        services, subgraph, subgraph_alt=subgraph_alt,
        alt_subgraphs=alt_subgraphs, agentic=agentic,
    ))
    g.add_node("execute_sql", make_execute_sql(services.connectors, max_retries=MAX_REFLECT_RETRIES))
    g.add_node("select", make_select_consensus(services.connectors, max_retries=MAX_REFLECT_RETRIES))
    g.add_node("validate", make_validate_rules(max_retries=MAX_REFLECT_RETRIES))
    g.add_node("reflect", make_reflect(services.llm, services.config or AgentConfig(), max_retries=MAX_REFLECT_RETRIES))
    g.add_node("output", output)

    _add_intent_routing(g, services)
    if clarify:
        g.add_node("clarify", make_clarify())
        g.add_edge("schema_linking", "clarify")
        if planner:
            g.add_node("planner", make_planner(services.llm, services.config or AgentConfig(), agentic=agentic, connectors=services.connectors))
            g.add_conditional_edges(
                "clarify",
                _route_after_clarify_planner,
                {"planner": "planner", "output": "output"},
            )
            g.add_edge("planner", "gen_sql")
        else:
            g.add_conditional_edges(
                "clarify",
                _route_after_clarify_gen_sql,
                {"gen_sql": "gen_sql", "output": "output"},
            )
    else:
        if planner:
            g.add_node("planner", make_planner(services.llm, services.config or AgentConfig(), agentic=agentic, connectors=services.connectors))
            g.add_edge("schema_linking", "planner")
            g.add_edge("planner", "gen_sql")
        else:
            g.add_edge("schema_linking", "gen_sql")
    g.add_edge("gen_sql", "execute_sql")
    g.add_edge("execute_sql", "select")
    g.add_edge("select", "validate")
    # Rollback ladder mirrors the graph topology: without the planner node,
    # the judge can never escalate to it.
    rollback_ladder = (
        ("gen_sql", "planner", "schema_linking") if planner
        else ("gen_sql", "schema_linking")
    )
    g.add_node("analyze_error", make_analyze_error(
        services.llm, services.config or AgentConfig(), rollback_ladder=rollback_ladder,
    ))
    analyze_targets = {
        "gen_sql": "gen_sql",
        "answer_metadata": "answer_metadata",
        "output": "output",
    }
    if planner:
        analyze_targets["planner"] = "planner"
    analyze_targets["schema_linking"] = "schema_linking"
    g.add_conditional_edges(
        "analyze_error",
        _make_route_after_analyze_error(analyze_targets),
        analyze_targets,
    )
    g.add_conditional_edges(
        "validate",
        _route_after_execute,
        {"analyze_error": "analyze_error", "reflect": "reflect", "output": "output"},
    )
    g.add_conditional_edges(
        "reflect",
        _route_after_reflect,
        {"analyze_error": "analyze_error", "answer_metadata": "answer_metadata", "output": "output"},
    )
    g.add_edge("output", END)
    return g


def _build_fixed(
    services: GraphServices,
    subgraph: CompiledStateGraph,
    clarify: bool = False,
    agentic: bool = True,
) -> StateGraph:
    g = StateGraph(WorkflowState)
    g.add_node("schema_linking", make_schema_linking(
        services.catalog, kb=services.kb, connectors=services.connectors,
        fallback_all=not clarify,
        llm=services.llm, config=services.config or AgentConfig(),
    ))
    g.add_node("gen_sql", _make_gen_sql_node(services, subgraph, agentic=agentic))
    g.add_node("execute_sql", make_execute_sql(services.connectors, max_retries=MAX_REFLECT_RETRIES))
    g.add_node("validate", make_validate_rules(max_retries=MAX_REFLECT_RETRIES))
    g.add_node("output", output)

    _add_intent_routing(g, services)
    if clarify:
        g.add_node("clarify", make_clarify())
        g.add_edge("schema_linking", "clarify")
        g.add_conditional_edges(
            "clarify",
            _route_after_clarify_gen_sql,
            {"gen_sql": "gen_sql", "output": "output"},
        )
    else:
        g.add_edge("schema_linking", "gen_sql")
    g.add_edge("gen_sql", "execute_sql")
    g.add_edge("execute_sql", "validate")
    g.add_conditional_edges(
        "validate",
        _route_after_execute_fixed,
        {"gen_sql": "gen_sql", "output": "output"},
    )
    g.add_edge("output", END)
    return g


def _route_after_execute_fixed(state: WorkflowState) -> Literal["gen_sql", "output"]:
    """Fixed graph: execution failure regenerates (budget enforced in execute)."""
    if state.error:
        return "output"
    if state.error_feedback:
        return "gen_sql"
    return "output"


def _build_empty() -> StateGraph:
    g = StateGraph(WorkflowState)
    g.add_node("output", output)
    g.add_edge(START, "output")
    g.add_edge("output", END)
    return g
