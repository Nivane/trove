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
from trove.workflow.state import GenSQLState, WorkflowState

from trove.workflow.nodes.schema_linking import make_schema_linking
from trove.workflow.nodes.gen_sql import make_generate, make_validate
from trove.workflow.nodes.execute_sql import make_execute_sql
from trove.workflow.nodes.reflect import make_reflect
from trove.workflow.nodes.output import output

logger = get_logger(__name__)

MAX_REFLECT_RETRIES = 2
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
) -> CompiledStateGraph:
    """Build the gen_sql subgraph: generate → validate retry loop."""
    config = services.config or AgentConfig()

    def route_after_validate(state: GenSQLState) -> str:
        if state.error or not state.validation_errors:
            return END
        return "generate"

    g = StateGraph(GenSQLState)
    g.add_node("generate", make_generate(services.llm, config))
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
):
    """Main-graph wrapper around the gen_sql subgraph."""

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
        if services.kb is not None and datasource:
            await services.kb.ensure_synced(default_datasource=datasource)
            example_hits = await services.kb.search_examples(
                state.question, datasource, limit=3,
            )
            few_shots = [
                {"question": h.question, "sql": h.sql, "template": h.template}
                for h in example_hits
            ]
            term_notes = [
                {"term": h.term, "mapping": h.mapping, "definition": h.definition}
                for h in await services.kb.search_terms(state.question, datasource)
            ]

        sub_state = GenSQLState(
            question=state.question,
            schema_context=state.schema_context,
            dialect=dialect,
            reflect_reason=state.reason,
            error_feedback=state.error_feedback,
            history=state.history,
            few_shots=few_shots,
            term_notes=term_notes,
        )
        out = await subgraph.ainvoke(sub_state)

        update: dict[str, Any] = {"dialect": dialect}
        if out["sql"]:
            update["sql"] = out["sql"]
        if out["error"]:
            update["error"] = out["error"]
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
) -> dict[str, CompiledStateGraph]:
    """Build and compile the reflection / fixed / empty graphs.

    Args:
        services: Service bundle bound into node closures.
        checkpointer: Optional LangGraph checkpointer (None = in-memory only).

    Returns:
        Mapping of workflow name → compiled graph.
    """
    subgraph = build_gen_sql_subgraph(services)

    def compile(g: StateGraph) -> CompiledStateGraph:
        return g.compile(checkpointer=checkpointer) if checkpointer else g.compile()

    return {
        "reflection": compile(_build_reflection(services, subgraph)),
        "fixed": compile(_build_fixed(services, subgraph)),
        "empty": compile(_build_empty()),
    }


def _route_after_reflect(state: WorkflowState) -> Literal["gen_sql", "output"]:
    # Termination is guaranteed by reflect itself: it only returns RETRY
    # while retry_count < MAX_REFLECT_RETRIES (then forces OK).
    if state.error or state.verdict != "RETRY":
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
) -> StateGraph:
    g = StateGraph(WorkflowState)
    g.add_node("schema_linking", make_schema_linking(
        services.catalog, kb=services.kb, connectors=services.connectors,
    ))
    g.add_node("gen_sql", _make_gen_sql_node(services, subgraph))
    g.add_node("execute_sql", make_execute_sql(services.connectors))
    g.add_node("reflect", make_reflect(services.llm, services.config or AgentConfig()))
    g.add_node("output", output)

    g.add_edge(START, "schema_linking")
    g.add_edge("schema_linking", "gen_sql")
    g.add_edge("gen_sql", "execute_sql")
    g.add_conditional_edges(
        "execute_sql",
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
) -> StateGraph:
    g = StateGraph(WorkflowState)
    g.add_node("schema_linking", make_schema_linking(
        services.catalog, kb=services.kb, connectors=services.connectors,
    ))
    g.add_node("gen_sql", _make_gen_sql_node(services, subgraph))
    g.add_node("execute_sql", make_execute_sql(services.connectors))
    g.add_node("output", output)

    g.add_edge(START, "schema_linking")
    g.add_edge("schema_linking", "gen_sql")
    g.add_edge("gen_sql", "execute_sql")
    g.add_conditional_edges(
        "execute_sql",
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
