"""Planner node — LLM drafts a concise query plan before SQL generation.

The plan (tables, joins, aggregations, filters, ordering) is injected
into the gen_sql prompt as a "Query plan" section — the two-step
plan-then-write flow. Planner failures are silent (empty plan): the
pipeline never blocks on planning.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

PLANNER_SYSTEM_PROMPT = """You are a SQL query planner. Given the user question and the relevant schema, draft a concise query plan covering: tables and joins, aggregations, filters, and ordering. One short paragraph, no SQL, no markdown."""


def make_planner(
    llm: LLMGateway,
    config: AgentConfig,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the planner node bound to an LLM gateway."""

    async def planner(state: WorkflowState) -> dict[str, Any]:
        # Upstream failure — pass through
        if state.error:
            return {}

        prompt_parts = [
            f"Question: {state.question}",
            f"Schema context:\n{state.schema_context[:1500]}",
        ]
        if state.history:
            prompt_parts.append(f"Conversation history:\n{state.history}")
        prompt = "\n\n".join(prompt_parts)

        try:
            model = config.target or "openai/gpt-4o"
            response = await llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                metadata={
                    "node": "planner",
                    "session_id": state.session_id,
                    "question": state.question[:80],
                },
            )
            plan = response.strip()
            return {"plan": plan} if plan else {}
        except Exception as e:
            logger.warning("Planner failed (proceeding without a plan): %s", e)
            return {}

    return planner
