"""Planner node — LLM drafts a concise query plan before SQL generation.

The plan (tables, joins, aggregations, filters, ordering) is injected
into the gen_sql prompt as a "Query plan" section — the two-step
plan-then-write flow. Planner failures are silent (empty plan): the
pipeline never blocks on planning.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.i18n import L, detect_language
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.llm.agent_loop import run_agent_loop
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

PLANNER_SYSTEM_PROMPT_ZH = """你是 SQL 查询规划器。根据用户问题和相关表结构，起草一份简洁的查询计划，覆盖：涉及的表与关联方式、聚合逻辑、过滤条件、排序。一段话即可，不要输出 SQL，不要用 markdown。"""

PLANNER_SYSTEM_PROMPT = """You are a SQL query planner. Given the user question and the relevant schema, draft a concise query plan covering: tables and joins, aggregations, filters, and ordering. One short paragraph, no SQL, no markdown."""


def make_planner(
    llm: LLMGateway,
    config: AgentConfig,
    agentic: bool = True,
    connectors=None,
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
            system_prompt = L(
                detect_language(state.question),
                PLANNER_SYSTEM_PROMPT_ZH,
                PLANNER_SYSTEM_PROMPT,
            )
            if agentic and connectors is not None:
                async def table_columns(arguments: dict) -> str:
                    table = arguments.get("table", "")
                    schema = await connectors.get_schema()
                    for t in schema.tables:
                        if t.name.lower() == table.lower():
                            return ", ".join(f"{c.name} {c.type}" for c in t.columns)
                    return f"table '{table}' not found"

                result = await run_agent_loop(
                    llm, model,
                    system=system_prompt,
                    user=prompt,
                    tools=[{
                        "type": "function",
                        "function": {
                            "name": "get_table_columns",
                            "description": "Inspect the columns of one table.",
                            "parameters": {
                                "type": "object",
                                "properties": {"table": {"type": "string"}},
                                "required": ["table"],
                            },
                        },
                    }],
                    tool_handlers={"get_table_columns": table_columns},
                    max_rounds=5,
                    metadata={"node": "planner", "session_id": state.session_id, "run_id": state.run_id},
                )
                plan = result["content"].strip()
                return {"plan": plan} if plan else {}

            start = time.monotonic()
            response = await llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                metadata={
                    "node": "planner",
                    "session_id": state.session_id,
                    "run_id": state.run_id,
                    "question": state.question[:80],
                },
            )
            plan = response.strip()
            llm_detail = {
                "model": model,
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "input_preview": prompt[:200],
                "output_preview": plan[:200],
            }
            return {"plan": plan, "llm": llm_detail} if plan else {}
        except Exception as e:
            logger.warning("Planner failed (proceeding without a plan): %s", e)
            return {}

    return planner
