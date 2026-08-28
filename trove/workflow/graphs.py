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
from trove.workflow.context_budget import (
    ContextItem,
    assemble_context,
    count_tokens,
    estimate_tokens,
)
from trove.workflow.context_score import history_items, relevance_score
from trove.workflow.schema_budget import trim_schema
from trove.llm.token_calibration import factor as token_calibration_factor
from trove.llm.token_calibration import record as record_token_calibration
from trove.workflow.complexity import grade_complexity
from trove.workflow.state import GenSQLState, WorkflowState

from trove.workflow.nodes.schema_linking import make_schema_linking
from trove.workflow.nodes.refuse import make_refuse
from trove.workflow.nodes.parse_date import make_parse_date
from trove.workflow.nodes.clarify import make_clarify
from trove.workflow.nodes.planner import make_planner
from trove.workflow.nodes.gen_sql import (
    build_sql_prompt_from_state,
    make_generate,
    make_validate,
    render_cache_prefix,
    render_entities,
    render_lessons,
    render_metrics,
    render_rules,
    render_shots,
    render_terms,
    render_user_facts,
)
from trove.prompts import render
from trove.workflow.nodes.fast_match import make_fast_match
from trove.workflow.nodes.execute_sql import make_execute_sql
from trove.workflow.nodes.select import make_select_consensus
from trove.workflow.nodes.validate import make_validate_rules
from trove.workflow.nodes.reflect import make_reflect
from trove.workflow.nodes.output import output
from trove.workflow.nodes.semantics import make_semantics
from trove.workflow.nodes.hitl import make_hitl
from trove.workflow.nodes.insights import make_insights
from trove.workflow.nodes.conclusion import make_conclusion
from trove.workflow.nodes.chart import make_chart
from trove.workflow.nodes.answer import make_answer_metadata
from trove.workflow.nodes.metadata_check import make_metadata_check
from trove.workflow.nodes.analyze_error import make_analyze_error, render_reasoning_context
from trove.workflow.nodes.terminal import (
    answer_chitchat,
    answer_correction,
    answer_reject,
)
from trove.core.i18n import L
from trove.workflow.intent import (
    Intent,
    classify_intent,
    has_followup_signal,
    has_strong_chitchat,
    has_strong_correction,
    has_strong_write,
    has_weak_signal,
    last_user_question,
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
CONTEXT_BUDGET_TOKENS = 2500  # gen prompt 可选块（示例/术语/教训/计划/历史）的预算（standard 档）
# 复杂度档位预算：simple 瘦身 / standard 默认 / complex 放开。
# 修正轮强制 standard（见 grade_complexity），档位只作用于首轮生成。
COMPLEXITY_BUDGET_TOKENS = {"simple": 1500, "standard": 2500, "complex": 4000}
# Schema 段（gen prompt 的稳定缓存前缀）自身的 token 上限：大库宽表下
# 避免 schema 全量注入挤占窗口；被裁的表经 lookup_schema 工具懒加载。
SCHEMA_BUDGET_TOKENS = {"simple": 900, "standard": 1800, "complex": 3500}


def generation_temperature(retry_count: int) -> float:
    """修正轮升温：温度 0 的确定性生成会让重试产出相同 SQL。"""
    return min(0.3, retry_count * 0.1)


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
    user_facts: Any | None = None  # optional user-level memory (per-user facts)
    semantic_layer: Any | None = None  # optional live semantic provider (OSSIE)
    lineage: Any | None = None  # optional data lineage service (metadata + execute recording)


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


def _candidate_schedule(n_alt: int) -> list[tuple[float, str]]:
    """去相关候选配置:温度 + 风格提示,确定性生成。

    scaling=5 时精确保持历史 4 个温度子图(0.3/0.5/0.7/1.0,无风格
    提示)——既有行为逐字节不变。更大规模(n=49/199):温度在 [0.2, 1.2]
    均匀铺开 + 风格提示轮换,让大候选池覆盖不同的正确解释表述,
    而不是温度差太小导致几乎同构的重复样本(select 只会按 SQL 归一
    文本去重,重复候选取不到额外的票)。

    Returns:
        [(temperature, style_mode), ...] 每候选一个配置。
    """
    if n_alt == 4:
        return [(float(t), "") for t in (0.3, 0.5, 0.7, 1.0)]
    modes = ["", "cte", "explicit-join", "subquery"]
    out: list[tuple[float, str]] = []
    for i in range(n_alt):
        frac = (i + 0.5) / n_alt
        temp = round(0.2 + 1.0 * frac, 3)
        out.append((temp, modes[i % len(modes)]))
    return out


_STYLE_HINTS = {
    "cte": "Prefer a WITH (CTE) formulation where it makes the query clearer.",
    "explicit-join": "Prefer explicit JOIN ... ON ... syntax over comma joins.",
    "subquery": "Prefer a single statement with subqueries where possible.",
}


async def _run_candidate_subagent(
    services,
    state: WorkflowState,
    sub_state: GenSQLState,
    dialect: str,
    *,
    temperature: float,
    mode: str = "",
    rotation: int = 1,
) -> str | None:
    """独立 subagent 并行打磨单个备选候选("主 agent 规划 + subagent 执行")。

    每个备选 = 一个独立的 ReAct loop(自建工具注册表、自定终止),以去相关
    温度 + few-shot 轮换 + 风格提示生成候选 SQL,供 select 共识投票。
    失败/空手/护栏降级返回 None——静默跳过,保持经典候选路径的容错语义。
    """
    from trove.workflow.nodes.gen_sql import build_sql_registry, extract_sql

    rotated = _rotate_few_shots(sub_state, rotation)
    registry = build_sql_registry(
        services.connectors, rotated.question, rotated.lang, dialect,
        matched_tables=state.matched_tables or None,
        datasource=state.datasource,
        semantic_only=bool(getattr(services.config, "semantic_first", False))
        and not bool(getattr(services, "catalog_probing", False)),
    )
    prompt = build_sql_prompt_from_state(rotated)
    hint = _STYLE_HINTS.get(mode)
    if hint:
        prompt = f"{prompt}\n\n{hint}"
    model = services.config.model_for(rotated.complexity) if services.config else "openai/gpt-4o"
    try:
        result = await run_agent_loop(
            services.llm, model,
            system=render("gen_sql/system", lang=rotated.lang),
            user=prompt,
            registry=registry,
            tool_timeout_s=20.0,
            time_budget_s=120.0,
            max_rounds=4,
            max_total_tokens=2500,
            metadata={
                "node": "gen_sql_subagent",
                "session_id": state.session_id,
                "run_id": state.run_id,
            },
            temperature=temperature,
        )
    except Exception:
        return None
    sql = extract_sql(result.get("content", "")) if result.get("content") else ""
    if not sql:
        # 模型可能在工具里给出 SQL(validate/probe/check/finish 过都算)
        for entry in reversed(result.get("tool_history") or []):
            args = entry.get("arguments") or {}
            payload = args.get("sql") or args.get("answer")
            if entry.get("name") in ("validate_sql", "probe_query", "check_result", "finish") and payload:
                sql = payload
                break
    return sql or None


# ── gen_sql subgraph ─────────────────────────────────────


def build_gen_sql_subgraph(
    services: GraphServices,
    max_retries: int = DEFAULT_GEN_SQL_RETRIES,
    temperature: float = 0.0,
    mode: str = "",
) -> CompiledStateGraph:
    """Build the gen_sql subgraph: generate → validate retry loop.

    Args:
        temperature: Sampling temperature (alternative candidates use
            a higher value for diversity).
        mode: Optional de-correlation style hint ("cte"/"explicit-join"/
            "subquery") appended to the generation prompt — keeps large
            candidate pools from converging on identical formulations.
    """
    config = services.config or AgentConfig()

    def route_after_validate(state: GenSQLState) -> str:
        if state.error or not state.validation_errors:
            return END
        return "generate"

    g = StateGraph(GenSQLState)
    g.add_node("generate", make_generate(services.llm, config, temperature=temperature, mode=mode))
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
                adapter = await services.connectors.get(state.datasource or None)
                dialect = adapter.dialect()
            except Exception:
                pass

        # Knowledge base: reference examples + terminology for the prompt.
        # Scoped to the active datasource; without a datasource context the
        # KB is not consulted.
        datasource = ""
        if services.connectors is not None:
            datasource = state.datasource or services.connectors.default_name or ""

        few_shots: list[dict[str, Any]] = []
        term_notes: list[dict[str, Any]] = []
        example_hits = []
        lessons: list[dict[str, Any]] = []
        rules: list[str] = []
        metric_hits: list[Any] = []
        entity_hits: list[Any] = []
        # 证据层锚:schema linking 的 matched_tables(实时语义层也要用)
        matched = list(state.matched_tables or [])
        # 复杂度分级(纯函数,零 IO/LLM):结构信号(plan) + 语义信号(锚定表)。
        # 修正轮强制 standard,保证修正循环行为不变;simple 用于跳过多候选等。
        in_correction = bool(
            state.error_feedback or state.error_analysis or state.reason
        )
        complexity = "standard" if in_correction else grade_complexity(
            state.plan_json, matched,
            term_hit=bool(state.intent_evidence.get("term_hit")),
            kb_hit=bool(state.kb_hits),
            plan_validation=state.plan_validation,
        )
        all_table_names: list[str] = []
        if services.catalog is not None and matched:
            try:
                all_table_names = [
                    t["name"] for t in await services.catalog.list_tables(datasource)
                ]
            except Exception:
                all_table_names = []
        if services.kb is not None and datasource:
            # ensure_synced 必须领先所有读(mtime 检查/懒加载);其后的四个
            # KB 调用互相独立(各自开自己的 aiosqlite 连接)→ asyncio.gather
            # 并行化。失败语义不变:KB 异常原本未捕获直接中止节点,
            # 现在按原调用顺序 re-raise 第一个异常。
            await services.kb.ensure_synced(default_datasource=datasource)
            # limit=5:检索注入 5 个示例(实测 3 个时注入列覆盖仅 28%,
            # 5 个 → 42%,token 成本 ~+60,预算(2500)完全装得下)
            _kb_results = await asyncio.gather(
                services.kb.search_examples(
                    state.question, datasource, limit=5,
                    tables=matched or None, all_tables=all_table_names or None,
                    # 多表锚定:每表分组 top 再合并,避免单表模板挤占
                    per_table=bool(matched),
                ),
                services.kb.list_rules(datasource),
                # Hint Bank 语义检索:embedding 相似度 × 投票加权 × 时效衰减,
                # 表锚过滤在检索内完成;不再用纯子串过滤(近义改写也能命中)
                services.kb.search_lessons(
                    state.question, datasource, limit=3,
                    tables=matched or None, all_tables=all_table_names or None,
                ),
                services.kb.search_terms(
                    state.question, datasource,
                    tables=matched or None, all_tables=all_table_names or None,
                ),
                # typed corpus 检索:指标(表达式+口径)与维度/枚举实体(值确认)
                services.kb.search_metrics(
                    state.question, datasource,
                    tables=matched or None, all_tables=all_table_names or None,
                    limit=4,
                ),
                services.kb.search_entities(
                    state.question, datasource,
                    tables=matched or None, all_tables=all_table_names or None,
                    limit=8,
                ),
                return_exceptions=True,
            )
            for r in _kb_results[1:]:  # 首个异常(原顺序)→ 中止节点,同旧语义
                if isinstance(r, BaseException):
                    raise r
            example_hits, rules, lessons, term_hits, metric_hits, entity_hits = _kb_results
            few_shots = [
                {"question": h.question, "sql": h.sql, "template": h.template}
                for h in example_hits
            ]
            term_notes = [
                {"term": h.term, "mapping": h.mapping, "definition": h.definition}
                for h in term_hits
            ]

        # 实时语义层 metric 作为 term 源(KB 优先,同名去重):
        # 与 KB term 同语义的名称/同义词匹配,表锚定由 provider 完成。
        # 不进 kb_exact_match 快捷通道(那是 KB 示例的专用路径)。
        if services.semantic_layer is not None and datasource:
            try:
                live_hits = services.semantic_layer.terms_for(
                    state.question, tables=matched or None,
                    all_tables=all_table_names or None,
                )
            except Exception as e:
                logger.warning("Semantic layer terms failed (%s): %s", datasource, e)
                live_hits = []
            seen = {t["term"] for t in term_notes}
            for h in live_hits:
                if h.term not in seen:
                    term_notes.append(
                        {"term": h.term, "mapping": h.mapping, "definition": h.definition})
                    seen.add(h.term)

        # 用户级记忆(用户+数据源作用域):检索该用户在当前数据源下的
        # 偏好/口径事实,按与问句的相关度取前 N 条注入 personalization
        # 上下文块。故障静默降级——记忆检索不得阻断生成(与 KB 同语义:
        # 检索失败不算生成失败)。
        user_fact_items: list[dict[str, Any]] = []
        if services.user_facts is not None and datasource and state.user_id:
            try:
                user_fact_items = await services.user_facts.search(
                    state.user_id, datasource, state.question, limit=3,
                )
            except Exception as e:
                logger.warning(
                    "User facts retrieval failed (%s): %s", datasource, e)

        # KB 精确命中:示例问题与当前问题几乎逐词一致 → 直接采用示例 SQL。
        # KB 保存的是该数据源的标准写法;对已收录的问题让模型"再解释一遍"
        # 只会产出歧义变体(实测:disp vs client 口径摇摆 5 轮)。SQL 仍会
        # 走 execute → validate → reflect,确定性规则与裁决不受影响。
        # 修正轮禁用快捷通道:KB 标准写法已被证明失败(执行/规则/裁决),
        # 再重发同一 SQL 只会空转烧预算——此时走生成,示例照常进 few_shots,
        # 模型基于错误反馈做定点修复。
        kb_exact_match: dict[str, Any] | None = None
        if not in_correction and example_hits:
            for h in example_hits:
                if _word_overlap(state.question, h.question) >= KB_EXACT_OVERLAP and h.sql:
                    kb_exact_match = {"question": h.question, "sql": h.sql}
                    break

        # Context budget: optional blocks filled by priority; within each
        # block, items are selected by relevance score, all capped at a
        # complexity-tiered token budget (simple 瘦身 / complex 放开).
        # Usage reported for observability (what the model actually saw).
        budget = COMPLEXITY_BUDGET_TOKENS.get(complexity, CONTEXT_BUDGET_TOKENS)
        optional_blocks: dict[str, list[ContextItem]] = {}
        if few_shots:
            # 每表/每条示例带 KB 相关度分数(score 降序=检索顺序)——预算
            # 内保留最相关条目,而非整块丢弃。getattr 兼容无 score 的桩。
            optional_blocks["few_shots"] = [
                ContextItem(
                    key=f"shot{i}",
                    text=render_shots([s]),
                    score=float(getattr(h, "score", 0) or 0),
                )
                for i, (h, s) in enumerate(zip(example_hits, few_shots))
            ]
        if rules:
            # rules/terms/lessons 无检索分数(0.0 = 纯检索序,裁剪无区分度)
            # ——用词重叠相关度补上,item 级裁剪才真正生效
            optional_blocks["rules"] = [
                ContextItem(
                    key=f"rule{i}",
                    text=render_rules([r]),
                    score=relevance_score(r, state.question),
                )
                for i, r in enumerate(rules)
            ]
        if term_notes:
            optional_blocks["term_notes"] = [
                ContextItem(
                    key=f"term{i}",
                    text=render_terms([t]),
                    score=relevance_score(
                        " ".join([str(t.get("term", "")), str(t.get("mapping", "")),
                                  str(t.get("definition", ""))]),
                        state.question,
                    ),
                )
                for i, t in enumerate(term_notes)
            ]
        if metric_hits:
            metric_items = [
                {
                    "name": m.name, "expression": m.expression,
                    "definition": m.definition, "score": m.score,
                }
                for m in metric_hits
            ]
            optional_blocks["metrics"] = [
                ContextItem(
                    key=f"metric{i}",
                    text=render_metrics([m]),
                    score=float(m["score"] or 0),
                )
                for i, m in enumerate(metric_items)
            ]
        if entity_hits:
            entity_items = [
                {
                    "field": e.field, "dataset": e.dataset, "role": e.role,
                    "enum_values": e.enum_values, "enum_labels": e.enum_labels,
                    "score": e.score,
                }
                for e in entity_hits
            ]
            optional_blocks["entities"] = [
                ContextItem(
                    key=f"entity{i}",
                    text=render_entities([e]),
                    score=float(e["score"] or 0),
                )
                for i, e in enumerate(entity_items)
            ]
        if lessons:
            optional_blocks["lessons"] = [
                ContextItem(
                    key=f"lesson{i}",
                    text=render_lessons([l]),
                    score=float(l.get("score") or relevance_score(
                        " ".join([str(l.get("pattern", "")), str(l.get("note", "")),
                                  str(l.get("sql_snippet", ""))]),
                        state.question,
                    ) or 0),
                )
                for i, l in enumerate(lessons)
            ]
        if user_fact_items:
            optional_blocks["user_facts"] = [
                ContextItem(
                    key=f"ufact{i}",
                    text=render_user_facts([f]),
                    score=relevance_score(str(f.get("fact", "")), state.question),
                )
                for i, f in enumerate(user_fact_items)
            ]
        if state.plan:
            optional_blocks["plan"] = [
                ContextItem(key="plan", text=state.plan, score=0.0),
            ]
        if state.history:
            # 历史拆成逐轮条目:score = 相关度(与问句词重叠) + 最近度——
            # 预算内保留最相关的轮次,而非整块全有全无
            optional_blocks["history"] = history_items(state.history, state.question)

        # 估算校准:实测/估算比例(按 model+dialect 的 EMA)放大单条成本,
        # 把系统性的低估反馈进预算,避免真实 prompt 超窗。冷启动 = 1.0。
        # 复杂度分档选模:simple/standard → model_fast,complex → target。
        model = services.config.model_for(complexity) if services.config else "openai/gpt-4o"
        cal = token_calibration_factor(model, dialect)

        def _count(text: str) -> int:
            n = count_tokens(text)
            return max(1, int(n * cal)) if cal > 0 else n

        included, context_usage = assemble_context(
            optional_blocks,
            {"few_shots": 1, "user_facts": 2, "rules": 3, "term_notes": 4,
             "metrics": 5, "entities": 6, "lessons": 7, "plan": 8, "history": 9},
            budget,
            count=_count,
        )
        # 按预算选中的 item key 过滤各源列表(保留检索顺序)
        def _trim(block: str, prefix: str, items: list[Any]) -> list[Any]:
            keys = set(included.get(block, ()))
            return [it for i, it in enumerate(items) if f"{prefix}{i}" in keys]

        few_shots = _trim("few_shots", "shot", few_shots)
        term_notes = _trim("term_notes", "term", term_notes)
        lessons = _trim("lessons", "lesson", lessons)
        rules = _trim("rules", "rule", rules)
        user_facts = _trim("user_facts", "ufact", user_fact_items)
        metrics = _trim("metrics", "metric", metric_items) if metric_hits else None
        entities = _trim("entities", "entity", entity_items) if entity_hits else None
        # history 是逐轮条目:按预算保留的轮次重拼回字符串注入
        history_trimmed = ""
        if "history" in included:
            kept_texts = [
                it.text for it in optional_blocks["history"]
                if it.key in set(included["history"])
            ]
            history_trimmed = "\n".join(kept_texts)

        # Schema 预算化:表块按信号裁剪到复杂度分档的 schema 上限,尾部
        # 段(值提示/语义)始终保留;被裁的表经 lookup_schema 工具懒加载。
        schema_budget = SCHEMA_BUDGET_TOKENS.get(complexity, SCHEMA_BUDGET_TOKENS["standard"])
        schema_for_gen = trim_schema(
            state.schema_context, schema_budget, state.question, state.plan,
        )

        sub_state = GenSQLState.from_workflow(
            state,
            dialect=dialect,
            included=set(included.keys()),
            reasoning_context=render_reasoning_context(
                state.reasoning_history, ("gen_sql",),
            ),
            few_shots=few_shots,
            term_notes=term_notes,
            lessons=lessons,
            rules=rules,
            user_facts=user_facts,
            metrics=metrics,
            entities=entities,
            history=history_trimmed,
            schema_context=schema_for_gen,
        )
        # 稳定可缓存前缀(dialect+schema)的 token 数——prompt caching 观测:
        # 同一数据源+方言下跨调用字节级一致,值越大缓存复用空间越大。
        cache_prefix_tokens = estimate_tokens(
            render_cache_prefix(dialect, schema_for_gen),
        )
        update: dict[str, Any] = {
            "dialect": dialect,
            "candidates": [],
            "context_usage": context_usage,
            "cache_prefix_tokens": cache_prefix_tokens,
            "complexity": complexity,
            # gen_sql 是权威生成路径:清掉陈旧快径标记,重生成的修正轮
            # 不享受快径的 reflect 跳过
            "fast_path": False,
        }

        if kb_exact_match is not None:
            # KB 精确命中:直接用标准 SQL,跳过模型生成(避免歧义变体)
            update["sql"] = kb_exact_match["sql"]
            update["kb_exact_match"] = True
            logger.info(
                "KB exact match used: %r", kb_exact_match["question"][:80],
            )
        elif agentic and complexity != "simple":
            from trove.workflow.nodes.gen_sql import (
                extract_sql, build_sql_registry,
            )

            # 工具统一由注册表提供(定义+处理器+finish 协议+超时/并行策略);
            # check_hits 收集 check_result 的规则命中,循环结束后随 update
            # 带出(归因)。validate_sql 始终可用,probe/check 依赖 connectors。
            # 语义优先(Phase B,决策 1):semantic_only 移除元数据枚举工具
            # (search_values / lookup_schema / explain_plan)。
            registry = build_sql_registry(
                services.connectors, sub_state.question, sub_state.lang, dialect,
                matched_tables=state.matched_tables or None,
                datasource=state.datasource,
                semantic_only=bool(getattr(services.config, "semantic_first", False))
                and not bool(getattr(services, "catalog_probing", False)),
            )

            prompt = build_sql_prompt_from_state(sub_state)
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
                registry=registry,
                tool_timeout_s=20.0,
                time_budget_s=120.0,
                max_rounds=4,
                max_total_tokens=2500,
                metadata={"node": "gen_sql", "session_id": state.session_id, "run_id": state.run_id},
                temperature=generation_temperature(state.retry_count),
            )
            except Exception as e:
                logger.warning("Agentic gen_sql failed (%s); falling back to classic", e)
                result = None

            async def _classic_fallback() -> None:
                """经典单发子图生成(异常 / agent loop 空手而归 / 护栏降级兜底)。"""
                out = await subgraph.ainvoke(sub_state)
                if out["sql"]:
                    update["sql"] = out["sql"]
                if out.get("attempts"):
                    update["attempts"] = out["attempts"]
                if out["error"]:
                    update["error"] = out["error"]

            if result is not None:
                # 估算校准闭环:首轮实测输入 tokens vs 可选块估算——系统性
                # 低估会经 token_calibration.factor 反馈进下一次装配��
                est = sum(u.get("tokens", 0) for u in context_usage if u.get("included"))
                actual = result.get("first_input_tokens") or 0
                if est and actual:
                    record_token_calibration(model, dialect, est, actual)
                sql = extract_sql(result["content"]) if result.get("content") else ""
                if not sql:
                    # 模型可能只在工具里给出 SQL,最终 content 无 SQL 回显
                    # (validate/probe/check/finish 过都算——捞最近一次工具载荷)。
                    for entry in reversed(result.get("tool_history") or []):
                        args = entry["arguments"] or {}
                        payload = args.get("sql") or args.get("answer")
                        if entry["name"] in ("validate_sql", "probe_query", "check_result", "finish") and payload:
                            sql = payload
                            break
                update["attempts"] = result["rounds"]
                # check_result 规则命中随状态带出(与既有 hits 合并,不覆盖
                # validate 层已记录的拦截)
                if registry.check_hits:
                    update["validation_hits"] = list(state.validation_hits) + registry.check_hits
                trail = " ".join(
                    p for p in (result.get("reasoning", ""), result.get("transcript", "")) if p
                )
                if trail:
                    update["reasoning_history"] = [{"node": "gen_sql", "text": trail[:800]}]
                if sql:
                    update["sql"] = sql
                elif result.get("guard_hit"):
                    # 护栏降级:agent loop 预算耗尽/原地打转未产出 SQL →
                    # 降到经典生成,而不是直接判失败
                    logger.warning(
                        "gen_sql agent loop guard (%s, %d rounds); degrading to classic",
                        result.get("budget_why"), result["rounds"],
                    )
                    await _classic_fallback()
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
            # 简单题(单表/单聚合/无 join 等)跳过多候选:共识投票是给
            # 歧义准备的,简单结构没有歧义值得花 4 次额外生成
            and complexity != "simple"
            # 修正轮默认跳过候选:模型已拿到错误反馈做定点修复,再花
            # 4 次生成纯属烧预算(执行/规则/裁决失败与"歧义"无关)。
            # 唯一例外是上一轮 select 平局(consensus=False)——候选池
            # 跨轮加票正是为那种歧义设计的,保留。
            and not (in_correction and state.consensus)
        ):
            alt_graphs = alt_subgraphs if alt_subgraphs else [subgraph_alt]
            # 候选池跨轮累积(重试=加票):旧候选保留,新候选加入后一起投票。
            # 平局轮打回 gen_sql 时 state.candidates 是上一轮的池——同组票数
            # 跨轮相加,正确解释的证据可以跨轮聚合,而不是每轮推倒重来。
            seen = {" ".join(update["sql"].split()).lower()}
            seen.update(" ".join(c.split()).lower() for c in state.candidates)
            candidates = list(state.candidates)
            schedule = _candidate_schedule(len(alt_graphs))
            if agentic:
                # subagent 委派(主 agent 规划 + subagent 并行执行):每个备选
                # 是一个独立 ReAct loop(自建工具注册表、自定终止),以去相关
                # 温度 + few-shot 轮换 + 风格提示并行打磨,产出候选供 select
                # 共识投票。asyncio.gather 把 N 个 subagent 压到单次耗时;
                # 容错:返回 None/异常即静默跳过。
                outs = await asyncio.gather(
                    *(
                        _run_candidate_subagent(
                            services, state, sub_state.model_copy(deep=True),
                            dialect, temperature=t, mode=m, rotation=idx + 1,
                        )
                        for idx, (t, m) in enumerate(schedule)
                    ),
                    return_exceptions=True,
                )
                for out_alt in outs:
                    if isinstance(out_alt, BaseException):
                        continue
                    alt_sql = out_alt or ""
                    if not alt_sql:
                        continue
                    key = " ".join(alt_sql.split()).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(alt_sql)
            else:
                # 经典候选:固定 generate→validate 子图并行(非 agentic 档)。
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
    scaling: int = 5,
) -> dict[str, CompiledStateGraph]:
    """Build and compile the reflection / fixed / empty graphs.

    Args:
        services: Service bundle bound into node closures.
        checkpointer: Optional LangGraph checkpointer (None = in-memory only).
        multi_candidate: Reflection generates alternative candidates at
            higher temperatures for consensus selection (off = single
            candidate).
        planner: Reflection drafts an LLM query plan before generation.
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
            services, subgraph, alt_subgraphs=alt_subgraphs, planner=planner,
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


def _route_after_reflect(state: WorkflowState) -> Literal["analyze_error", "answer_metadata", "insights"]:
    # Termination is guaranteed by reflect itself: it only returns RETRY
    # while retry_count < MAX_REFLECT_RETRIES (then forces OK).
    if state.error:
        return "insights"  # insights 对 error 透传,不阻断降级输出
    if state.no_sql:
        return "answer_metadata"
    if state.verdict != "RETRY":
        return "insights"
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
        followup = has_followup_signal(question, state.history)
        weak_sig = has_weak_signal(question)

        llm_intent: Intent | None = None
        llm_detail: dict[str, Any] | None = None
        llm_error = ""
        if llm is not None:
            # 意图分类是琐碎判别任务,走 fast 档(未配置 fast → 回退 target)
            model = (config.model_fast or config.target) if config else "openai/gpt-4o"
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
                chitchat_signal=chit_sig,
                correction_signal=corr_sig,
                followup_signal=followup,
                history_present=bool(state.history),
                weak_signal=weak_sig,
                mentioned_table=mentioned_table,
                term_hit=term_hit,
                data_signal=data_signal,
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
            "write_signal": write_sig,
            "chitchat_signal": chit_sig,
            "correction_signal": corr_sig,
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
        model = (config.model_fast or config.target) if config else "openai/gpt-4o"
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
    g.add_edge(START, "route_intent")
    g.add_conditional_edges(
        "route_intent",
        _route_after_intent,
        {
            "query": "parse_date",
            "metadata": "answer_metadata",
            "write": "answer_reject",
            "chitchat": "answer_chitchat",
            "correction": "answer_correction",
        },
    )
    g.add_edge("answer_reject", "output")
    g.add_edge("answer_chitchat", "output")
    g.add_edge("answer_correction", "output")
    g.add_edge("parse_date", "schema_linking")
    g.add_edge("answer_metadata", "metadata_check")
    g.add_conditional_edges(
        "metadata_check",
        _make_route_after_feedback("answer_metadata", "answer_metadata", "output"),
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
    工厂替代三份近似重复的路由。error 与 error_feedback 同目标(如 metadata
    图)时两者传同一个值。
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
    gen_sql/planner/schema_linking 正常重生成,重生成轮 in_correction
    保证不再触发快径。
    """

    def route(state: WorkflowState) -> str:
        if state.fast_path and state.sql and not state.error:
            return "execute_sql"
        return miss_target

    return route


def _route_after_clarify_planner(state: WorkflowState) -> Literal["planner", "output"]:
    """Clarification needed → ask the user; otherwise proceed to planning."""
    if state.error or state.clarification_question:
        return "output"
    return "planner"


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
) -> Literal["refuse", "gen_sql"]:
    if state.error or state.no_model or state.refusal:
        return "refuse"
    return "gen_sql"


def _route_after_planner(state: WorkflowState) -> Literal["refuse", "gen_sql"]:
    """Planner 后:编译 MISS / 无语义模型 → refuse;否则 gen_sql。"""
    if state.error or state.no_model or state.refusal:
        return "refuse"
    return "gen_sql"


def _route_after_clarify_gen_sql(state: WorkflowState) -> Literal["gen_sql", "output"]:
    """Clarification needed → ask the user; otherwise proceed to generation."""
    if state.error or state.clarification_question:
        return "output"
    return "gen_sql"


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
        kb=services.kb, connectors=services.connectors,
        semantic_layer=services.semantic_layer,
    ))
    g.add_node("gen_sql", _make_gen_sql_node(
        services, subgraph, subgraph_alt=subgraph_alt,
        alt_subgraphs=alt_subgraphs, agentic=agentic,
    ))
    g.add_node("execute_sql", make_execute_sql(services.connectors, max_retries=MAX_REFLECT_RETRIES, lineage=services.lineage))
    g.add_node("select", make_select_consensus(services.connectors, max_retries=MAX_REFLECT_RETRIES))
    g.add_node("validate", make_validate_rules(max_retries=MAX_REFLECT_RETRIES))
    g.add_node("reflect", make_reflect(services.llm, services.config or AgentConfig(), max_retries=MAX_REFLECT_RETRIES))
    # 说明语义 + 执行前人工确认(HITL):生成 SQL → 解释 → 确认 → 执行 → 洞察
    g.add_node("semantics", make_semantics(services.llm, services.config or AgentConfig()))
    g.add_node("hitl", make_hitl(services.config or AgentConfig()))
    g.add_node("insights", make_insights(services.llm, services.config or AgentConfig()))
    g.add_node("chart", make_chart(semantic_layer=services.semantic_layer))
    g.add_node("conclusion", make_conclusion(services.llm, services.config or AgentConfig()))
    g.add_node("output", output)

    _add_intent_routing(g, services)
    # 确定性简单快径:模板命中 → 直接执行,跳过 planner + 生成 + 裁决;
    # miss → 走正常 planner/gen_sql 路径(静默降级,零状态写入)。
    g.add_node("fast_match", make_fast_match(
        kb=services.kb, connectors=services.connectors,
        config=services.config or AgentConfig(),
    ))
    if clarify:
        g.add_node("clarify", make_clarify())
        g.add_node("refuse", make_refuse(services.llm, services.config or AgentConfig(), kb=services.kb, semantic_layer=services.semantic_layer))
        g.add_edge("refuse", "output")
        g.add_conditional_edges(
            "schema_linking",
            _route_semantic_gate_after_linking,
            {"refuse": "refuse", "clarify": "clarify"},
        )
        if planner:
            g.add_node("planner", make_planner(services.llm, services.config or AgentConfig(), connectors=services.connectors, semantic_layer=services.semantic_layer))
            g.add_conditional_edges(
                "clarify",
                _route_after_clarify_planner,
                {"planner": "fast_match", "output": "output"},
            )
            g.add_conditional_edges(
                "fast_match",
                _make_route_after_fast_match("planner"),
                {"execute_sql": "execute_sql", "planner": "planner"},
            )
            g.add_conditional_edges(
                "planner",
                _route_after_planner,
                {"refuse": "refuse", "gen_sql": "gen_sql"},
            )
        else:
            g.add_conditional_edges(
                "clarify",
                _route_after_clarify_gen_sql,
                {"gen_sql": "fast_match", "output": "output"},
            )
            g.add_conditional_edges(
                "fast_match",
                _make_route_after_fast_match("gen_sql"),
                {"execute_sql": "execute_sql", "gen_sql": "gen_sql"},
            )
    else:
        if planner:
            g.add_node("planner", make_planner(services.llm, services.config or AgentConfig(), connectors=services.connectors, semantic_layer=services.semantic_layer))
            g.add_node("refuse", make_refuse(services.llm, services.config or AgentConfig(), kb=services.kb, semantic_layer=services.semantic_layer))
            g.add_edge("refuse", "output")
            g.add_conditional_edges(
                "schema_linking",
                _route_semantic_gate_after_linking_fast_match,
                {"refuse": "refuse", "fast_match": "fast_match"},
            )
            g.add_conditional_edges(
                "fast_match",
                _make_route_after_fast_match("planner"),
                {"execute_sql": "execute_sql", "planner": "planner"},
            )
            g.add_conditional_edges(
                "planner",
                _route_after_planner,
                {"refuse": "refuse", "gen_sql": "gen_sql"},
            )
        else:
            g.add_node("refuse", make_refuse(services.llm, services.config or AgentConfig(), kb=services.kb, semantic_layer=services.semantic_layer))
            g.add_edge("refuse", "output")
            g.add_conditional_edges(
                "schema_linking",
                _route_semantic_gate_after_linking_fast_match,
                {"refuse": "refuse", "fast_match": "fast_match"},
            )
            g.add_conditional_edges(
                "fast_match",
                _make_route_after_fast_match("gen_sql"),
                {"execute_sql": "execute_sql", "gen_sql": "gen_sql"},
            )
    # 生成路径:gen_sql → semantics(说明语义) → hitl(人工确认) → execute。
    # hitl 被用户否决 → 直接 output(不执行);批准/未开启 → execute_sql。
    # 确定性快径(fast_match)仍直达 execute_sql,跳过生成级别的确认。
    g.add_edge("gen_sql", "semantics")
    g.add_edge("semantics", "hitl")
    g.add_conditional_edges(
        "hitl",
        _route_after_hitl,
        {"execute_sql": "execute_sql", "output": "output"},
    )
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
    # 执行/规则失败 → analyze_error 诊断 → 重生成。execute_sql 自守预算
    # (耗尽走 state.error、成功清 feedback),循环必然终止:error_feedback
    # 置位 ⇒ 诊断后重生成;下一轮要么清掉要么降级。
    g.add_conditional_edges(
        "validate",
        _make_route_after_feedback("output", "analyze_error", "reflect"),
        {"analyze_error": "analyze_error", "reflect": "reflect", "output": "output"},
    )
    g.add_conditional_edges(
        "reflect",
        _route_after_reflect,
        {"analyze_error": "analyze_error", "answer_metadata": "answer_metadata", "insights": "insights"},
    )
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
    g = StateGraph(WorkflowState)
    g.add_node("schema_linking", make_schema_linking(
        kb=services.kb, connectors=services.connectors,
        semantic_layer=services.semantic_layer,
    ))
    g.add_node("gen_sql", _make_gen_sql_node(services, subgraph, agentic=agentic))
    g.add_node("execute_sql", make_execute_sql(services.connectors, max_retries=MAX_REFLECT_RETRIES, lineage=services.lineage))
    g.add_node("validate", make_validate_rules(max_retries=MAX_REFLECT_RETRIES))
    # 说明语义 + 执行前人工确认(HITL) + 执行后洞察
    g.add_node("semantics", make_semantics(services.llm, services.config or AgentConfig()))
    g.add_node("hitl", make_hitl(services.config or AgentConfig()))
    g.add_node("insights", make_insights(services.llm, services.config or AgentConfig()))
    g.add_node("chart", make_chart(semantic_layer=services.semantic_layer))
    g.add_node("conclusion", make_conclusion(services.llm, services.config or AgentConfig()))
    g.add_node("output", output)

    _add_intent_routing(g, services)
    g.add_node("refuse", make_refuse(services.llm, services.config or AgentConfig(), kb=services.kb, semantic_layer=services.semantic_layer))
    g.add_edge("refuse", "output")
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
            {"gen_sql": "gen_sql", "output": "output"},
        )
    else:
        g.add_conditional_edges(
            "schema_linking",
            _route_semantic_gate_after_linking_gen_sql,
            {"refuse": "refuse", "gen_sql": "gen_sql"},
        )
    g.add_edge("gen_sql", "semantics")
    g.add_edge("semantics", "hitl")
    g.add_conditional_edges(
        "hitl",
        _route_after_hitl,
        {"execute_sql": "execute_sql", "output": "output"},
    )
    g.add_edge("execute_sql", "validate")
    g.add_conditional_edges(
        "validate",
        _make_route_after_feedback("output", "gen_sql", "insights"),
        {"gen_sql": "gen_sql", "insights": "insights", "output": "output"},
    )
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
