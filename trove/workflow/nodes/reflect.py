"""Reflect node — evaluates query result quality.

After SQL execution, this node:
1. Checks if the result makes sense for the original question
2. Decides: accept (OK/EMPTY) or retry (RETRY → main graph loops back
   to the gen_sql subgraph with the reason as context)

Max retries: 2 (hard limit to prevent infinite loops).

Node shape: `async def reflect(state: WorkflowState) -> dict`
returns a partial state update.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.llm.agent_loop import run_agent_loop
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

REFLECT_SYSTEM_PROMPT = """You are a SQL result evaluator. Your task is to check whether the query results correctly answer the user's question.

Evaluate on:
1. Does the result make logical sense for the question?
2. Are there actual rows returned (not empty when expected)?
3. Do the column names match what the question asks for?
4. Are the values in a reasonable range?

Respond with ONE of:
- "OK" — the result is satisfactory
- "RETRY: <reason>" — the result is wrong and needs regenerating
- "EMPTY" — the result is empty but the SQL looks correct (data might not exist)
"""

MAX_TOTAL_RETRIES = 10


def make_reflect(
    llm: LLMGateway,
    config: AgentConfig,
    max_retries: int = MAX_TOTAL_RETRIES,
    agentic: bool = True,
    connectors=None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the reflect node bound to an LLM gateway.

    Args:
        max_retries: Shared correction budget (RETRY verdicts are only
            issued while retry_count < max_retries).
    """

    async def reflect(state: WorkflowState) -> dict[str, Any]:
        # Upstream node failed — pass through without running
        if state.error:
            return {}

        # Fast path: empty result is acceptable (no data matches)
        if state.row_count == 0:
            return {
                "verdict": "EMPTY",
                "reason": "Query returned zero rows — this may be correct if no data matches",
            }

        prompt = _build_reflect_prompt(
            state.question, state.columns, state.rows[:10], state.row_count,
        )

        try:
            model = config.target or "openai/gpt-4o"
            if agentic and connectors is not None:
                from trove.workflow.rules import validate as run_rules

                async def re_execute(arguments: dict) -> str:
                    sql = arguments.get("sql", "")
                    try:
                        result = await connectors.execute(sql)
                    except Exception as e:
                        return f"ERROR: {e}"
                    observation = f"rows={result.row_count}, columns={result.columns}"
                    warning = run_rules(
                        state.question, sql, result.columns, result.rows, result.row_count,
                    )
                    if warning:
                        observation += f"\nRule warning: {warning}"
                    return observation

                result = await run_agent_loop(
                    llm, model,
                    system=REFLECT_SYSTEM_PROMPT,
                    user=prompt,
                    tools=[{
                        "type": "function",
                        "function": {
                            "name": "execute_sql",
                            "description": "Re-execute a SQL query to verify results.",
                            "parameters": {
                                "type": "object",
                                "properties": {"sql": {"type": "string"}},
                                "required": ["sql"],
                            },
                        },
                    }],
                    tool_handlers={"execute_sql": re_execute},
                    max_rounds=5,
                    metadata={"node": "reflect", "session_id": state.session_id, "run_id": state.run_id},
                )
                start = time.monotonic()
                response = result["content"]  # 供下游 llm_detail/verdict 处理
                verdict = result["content"].strip().upper()
            else:
                start = time.monotonic()
                response = await llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": REFLECT_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                metadata={
                    "node": "reflect",
                    "session_id": state.session_id,
                    "run_id": state.run_id,
                    "question": state.question[:80],
                },
            )
            verdict = response.strip().upper()
            llm_detail = {
                "model": model,
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "input_preview": prompt[:200],
                "output_preview": response[:200],
            }

            if verdict.startswith("OK"):
                return {"verdict": "OK", "llm": llm_detail}
            elif verdict.startswith("RETRY"):
                reason = response.replace("RETRY:", "").replace("RETRY", "").strip()
                if state.retry_count >= max_retries:
                    logger.warning(
                        "Max retries (%d) exceeded; accepting result despite issues",
                        max_retries,
                    )
                    return {"verdict": "OK", "forced": True, "reason": reason, "llm": llm_detail}

                return {
                    "verdict": "RETRY",
                    "reason": reason,
                    "retry_count": state.retry_count + 1,
                    "llm": llm_detail,
                }
            else:  # EMPTY or unknown
                return {"verdict": verdict, "llm": llm_detail}

        except Exception as e:
            # If reflection fails, assume OK (don't block on reflection)
            logger.warning("Reflection LLM call failed; assuming OK: %s", e)
            return {"verdict": "OK"}

    return reflect


def _build_reflect_prompt(
    question: str,
    columns: list[str],
    sample_rows: list[list],
    total_rows: int,
) -> str:
    """Build the reflection evaluation prompt."""
    sample = ""
    if sample_rows:
        sample = "\n".join(
            str(row) for row in sample_rows[:5]
        )

    return (
        f"User question: {question}\n\n"
        f"Result columns: {columns}\n"
        f"Total rows returned: {total_rows}\n"
        f"Sample rows (first 5):\n{sample}\n\n"
        f"Does this result correctly answer the user's question?\n"
        f"Respond with OK, RETRY: <reason>, or EMPTY."
    )
