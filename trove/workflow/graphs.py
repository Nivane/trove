"""LangGraph builders for Trove query workflows.

Composition:
  - gen_sql subgraph: generate → validate retry loop (max_retries attempts)
  - reflection main graph: schema_linking → gen_sql[subgraph] → execute_sql
    → reflect → (conditional) back to gen_sql (≤2 times) or output
  - fixed main graph: same pipeline without the reflect loop
  - empty main graph: pass-through output

Graceful degradation: node failures write state.error; every downstream
node passes through untouched, and the router sends the run to output,
which formats a readable error section.
"""

from __future__ import annotations

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
from trove.workflow.context_budget import assemble_blocks
from trove.workflow.state import GenSQLState, WorkflowState

from trove.workflow.nodes.schema_linking import make_schema_linking
from trove.workflow.nodes.clarify import make_clarify
from trove.workflow.nodes.planner import make_planner
from trove.workflow.nodes.gen_sql import make_generate, make_validate
from trove.workflow.nodes.execute_sql import make_execute_sql
from trove.workflow.nodes.select import make_select_consensus
from trove.workflow.nodes.validate import make_validate_rules
from trove.workflow.nodes.reflect import make_reflect
from trove.workflow.nodes.output import output
from trove.workflow.nodes.answer import make_answer_metadata
from trove.workflow.intent import INTENT_PROMPT, Intent, classify_intent, has_weak_signal, parse_llm_intent

logger = get_logger(__name__)

MAX_REFLECT_RETRIES = 10  # 修正轮上限（执行错误/规则/一致性/裁决共享）
CONTEXT_BUDGET_TOKENS = 2500  # gen prompt 可选块（示例/术语/教训/计划/历史）的预算
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
):
    """Main-graph wrapper around the gen_sql subgraph.

    With subgraph_alt given (multi-candidate mode), a second candidate
    is generated at a higher temperature and stored in state.candidates
    for the consensus select node.
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
            example_hits = await services.kb.search_examples(
                state.question, datasource, limit=3,
            )
            rules = await services.kb.list_rules(datasource)
            all_lessons = await services.kb.list_lessons(datasource)
            haystack = (state.question + " " + state.error_feedback).lower()
            lessons = [
                l for l in all_lessons
                if l.get("pattern", "").lower() in haystack
            ][:3]
            few_shots = [
                {"question": h.question, "sql": h.sql, "template": h.template}
                for h in example_hits
            ]
            term_notes = [
                {"term": h.term, "mapping": h.mapping, "definition": h.definition}
                for h in await services.kb.search_terms(state.question, datasource)
            ]

        # Context budget: optional blocks filled by priority, usage
        # reported for observability (what the model actually saw).
        optional_blocks = {
            "few_shots": _render_shots(few_shots),
            "rules": "\n".join(rules),
            "term_notes": _render_terms(term_notes),
            "lessons": _render_lessons(lessons),
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
            schema_context=state.schema_context,
            dialect=dialect,
            reflect_reason=state.reason,
            error_feedback=state.error_feedback,
            history=state.history if "history" in included else "",
            plan=state.plan if "plan" in included else "",
            few_shots=few_shots if "few_shots" in included else None,
            term_notes=term_notes if "term_notes" in included else None,
            lessons=lessons if "lessons" in included else None,
            rules=rules if "rules" in included else None,
        )
        out = await subgraph.ainvoke(sub_state)

        update: dict[str, Any] = {
            "dialect": dialect,
            "candidates": [],
            "context_usage": context_usage,
        }
        if out["sql"]:
            update["sql"] = out["sql"]
        if out.get("attempts"):
            update["attempts"] = out["attempts"]  # 子图内校验重试次数
        if out["error"]:
            update["error"] = out["error"]

        # Multi-candidate: a second generation at higher temperature;
        # failures fall back to the single-candidate path silently.
        if subgraph_alt is not None and out["sql"] and not out["error"]:
            try:
                out_alt = await subgraph_alt.ainvoke(sub_state.model_copy(deep=True))
            except Exception:
                out_alt = {}
            alt_sql = out_alt.get("sql", "")
            if alt_sql and not out_alt.get("error") and alt_sql != out["sql"]:
                update["candidates"] = [alt_sql]
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

    Returns:
        Mapping of workflow name → compiled graph.
    """
    subgraph = build_gen_sql_subgraph(services)
    subgraph_alt = (
        build_gen_sql_subgraph(services, temperature=0.3)
        if multi_candidate else None
    )

    def compile(g: StateGraph) -> CompiledStateGraph:
        return g.compile(checkpointer=checkpointer) if checkpointer else g.compile()

    return {
        "reflection": compile(_build_reflection(services, subgraph, subgraph_alt, planner, clarify)),
        "fixed": compile(_build_fixed(services, subgraph, clarify)),
        "empty": compile(_build_empty()),
    }


def _route_after_reflect(state: WorkflowState) -> Literal["gen_sql", "output"]:
    # Termination is guaranteed by reflect itself: it only returns RETRY
    # while retry_count < MAX_REFLECT_RETRIES (then forces OK).
    if state.error or state.verdict != "RETRY":
        return "output"
    return "gen_sql"


def make_route_intent(llm: LLMGateway | None = None, config: AgentConfig | None = None):
    """Intent router: strong signals first, LLM confirms weak signals."""

    async def route_intent(state: WorkflowState) -> dict[str, Any]:
        if state.error:
            return {}
        intent = classify_intent(state.question)
        if intent is None and has_weak_signal(state.question) and llm is not None:
            try:
                model = (config.target if config else "") or "openai/gpt-4o"
                response = await llm.chat(
                    model=model,
                    max_tokens=16,
                    messages=[
                        {"role": "system", "content": INTENT_PROMPT},
                        {"role": "user", "content": state.question},
                    ],
                    metadata={
                        "node": "route_intent",
                        "session_id": state.session_id,
                        "question": state.question[:80],
                    },
                )
                intent = parse_llm_intent(response)
            except Exception:
                intent = None
        if intent is None:
            intent = Intent.QUERY  # permissive default
        return {"intent": intent.value}

    return route_intent


def _route_after_intent(state: WorkflowState) -> str:
    """Dispatch by classified intent (falls back to query)."""
    return state.intent


def _add_intent_routing(g: StateGraph, services: GraphServices) -> None:
    """Shared wiring: START → route_intent → query pipeline or answer nodes."""
    g.add_node("route_intent", make_route_intent(services.llm, services.config))
    g.add_node("answer_metadata", make_answer_metadata(
        services.catalog, kb=services.kb, connectors=services.connectors,
    ))
    g.add_edge(START, "route_intent")
    g.add_conditional_edges(
        "route_intent",
        _route_after_intent,
        {
            "query": "schema_linking",
            "metadata": "answer_metadata",
        },
    )
    g.add_edge("answer_metadata", "output")


def _render_shots(shots: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"Q: {s.get('question', '')}\nSQL: {s.get('sql', '')}" for s in shots
    )


def _render_terms(terms: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- {t.get('term', '')} → {t.get('mapping', '')}" for t in terms
    )


def _render_lessons(lessons: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- {l.get('pattern', '')}: {l.get('note', '')}" for l in lessons
    )


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


def _route_after_execute(state: WorkflowState) -> Literal["gen_sql", "reflect", "output"]:
    """Execution failure feeds back to gen_sql.

    execute_sql enforces the budget itself (degrades via state.error when
    exhausted, clears feedback on success), so the loop always terminates:
    error_feedback set ⇒ regenerate; next failure either clears or degrades.
    """
    if state.error:
        return "output"
    if state.error_feedback:
        return "gen_sql"
    return "reflect"


def _build_reflection(
    services: GraphServices,
    subgraph: CompiledStateGraph,
    subgraph_alt: CompiledStateGraph | None = None,
    planner: bool = True,
    clarify: bool = False,
) -> StateGraph:
    g = StateGraph(WorkflowState)
    g.add_node("schema_linking", make_schema_linking(
        services.catalog, kb=services.kb, connectors=services.connectors,
    ))
    g.add_node("gen_sql", _make_gen_sql_node(services, subgraph, subgraph_alt))
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
            g.add_node("planner", make_planner(services.llm, services.config or AgentConfig()))
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
            g.add_node("planner", make_planner(services.llm, services.config or AgentConfig()))
            g.add_edge("schema_linking", "planner")
            g.add_edge("planner", "gen_sql")
        else:
            g.add_edge("schema_linking", "gen_sql")
    g.add_edge("gen_sql", "execute_sql")
    g.add_edge("execute_sql", "select")
    g.add_edge("select", "validate")
    g.add_conditional_edges(
        "validate",
        _route_after_execute,
        {"gen_sql": "gen_sql", "reflect": "reflect", "output": "output"},
    )
    g.add_conditional_edges(
        "reflect",
        _route_after_reflect,
        {"gen_sql": "gen_sql", "output": "output"},
    )
    g.add_edge("output", END)
    return g


def _build_fixed(
    services: GraphServices,
    subgraph: CompiledStateGraph,
    clarify: bool = False,
) -> StateGraph:
    g = StateGraph(WorkflowState)
    g.add_node("schema_linking", make_schema_linking(
        services.catalog, kb=services.kb, connectors=services.connectors,
    ))
    g.add_node("gen_sql", _make_gen_sql_node(services, subgraph))
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
