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
from trove.core.i18n import L, detect_language
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.llm.agent_loop import run_agent_loop
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

REFLECT_SYSTEM_PROMPT_ZH = """你是严格的 SQL 结果评估器。检查查询结果是否正确回答了用户的问题。

严格一点：静默的错误结果比可见的错误更糟。不确定时返回 RETRY 并给出具体理由。

评估要点：
1. 结果是否符合问题的逻辑？
2. 行数是否符合问题形态：计数/百分比类问题 → 单个数值；列表/Top-N 类问题 → 多行。
3. 空结果可疑：判断是 SQL 写错（join/过滤/表列名错误）还是数据确实没有匹配。只有 SQL 本身正确时才回答 EMPTY。
4. 列名是否符合问题所问？
5. 数值的量级、单位、业务逻辑是否合理（如年龄小于 120、百分比在 0-100）？

只回答以下之一：
- "OK" — 结果满意
- "RETRY: <理由>" — 结果错误，需要重新生成（说明哪里错了）
- "EMPTY" — 结果为空但 SQL 正确（数据确实无匹配）
"""

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
            system_prompt = L(
                detect_language(state.question),
                REFLECT_SYSTEM_PROMPT_ZH,
                REFLECT_SYSTEM_PROMPT,
            )
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
                    system=system_prompt,
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
                    {"role": "system", "content": system_prompt},
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
