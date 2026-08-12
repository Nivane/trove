"""Reflect node — evaluates query result quality.

After SQL execution, this node:
1. Checks if the result makes sense for the original question
2. Decides: accept (output) or retry (back to gen_sql)
3. In MVP, uses LLM to judge; could be rule-based for simple cases
"""

from __future__ import annotations

from trove.core.types import NodeStatus, WorkflowContext
from trove.core.logging import get_logger
from trove.workflow.node import AgenticNode, NodeResult, LLMLoopConfig
from trove.workflow.node_type import NodeType

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


class ReflectNode(AgenticNode):
    """Evaluate query results and decide whether to retry.

    If RETRY is returned, the engine routes back to gen_sql
    with the reflect reason as additional context.

    Max retries: 2 (hard limit to prevent infinite loops).
    """

    node_type = NodeType.REFLECT
    MAX_TOTAL_RETRIES = 2

    def __init__(self, name: str = "reflect"):
        config = LLMLoopConfig(
            system_prompt=REFLECT_SYSTEM_PROMPT,
            max_rounds=1,
        )
        super().__init__(name, config)

    async def execute(self, ctx: WorkflowContext) -> NodeResult:
        """Evaluate results from execute_sql.

        Returns:
            SUCCESS with verdict="OK" → proceed to output
            RETRY with verdict="RETRY" and reason → engine loops back
            SUCCESS with verdict="EMPTY" → proceed (empty is valid)
        """
        # Get execution results
        question = ctx.user_message.content
        exec_data = {}
        if hasattr(ctx, '_node_data'):
            exec_data = ctx._node_data.get("execute_sql", {})  # type: ignore[attr-defined]

        row_count = exec_data.get("row_count", -1)
        columns = exec_data.get("columns", [])
        rows = exec_data.get("rows", [])

        # Fast path: empty result is acceptable (no data matches)
        if row_count == 0:
            return NodeResult(
                node_name=self.name,
                status=NodeStatus.SUCCESS,
                data={
                    "verdict": "EMPTY",
                    "reason": "Query returned zero rows — this may be correct if no data matches",
                },
            )

        # Fast path: too many rows might indicate a problem
        if row_count > 10000:
            # This is acceptable but worth noting
            pass

        # LLM-based reflection
        prompt = self._build_reflect_prompt(question, columns, rows[:10], row_count)

        try:
            response = await self._call_llm(ctx, prompt)
            verdict = response.strip().upper()

            if verdict.startswith("OK"):
                return NodeResult(
                    node_name=self.name,
                    status=NodeStatus.SUCCESS,
                    data={"verdict": "OK"},
                )
            elif verdict.startswith("RETRY"):
                reason = response.replace("RETRY:", "").replace("RETRY", "").strip()
                retry_count = exec_data.get("_retry_count", 0)
                if retry_count >= self.MAX_TOTAL_RETRIES:
                    logger.warning(
                        "Max retries (%d) exceeded; accepting result despite issues",
                        self.MAX_TOTAL_RETRIES,
                    )
                    return NodeResult(
                        node_name=self.name,
                        status=NodeStatus.SUCCESS,
                        data={"verdict": "OK", "forced": True, "reason": reason},
                    )

                return NodeResult(
                    node_name=self.name,
                    status=NodeStatus.RETRY,
                    data={
                        "verdict": "RETRY",
                        "reason": reason,
                        "retry_target": "gen_sql",
                        "_retry_count": retry_count + 1,
                    },
                )
            else:  # EMPTY or unknown
                return NodeResult(
                    node_name=self.name,
                    status=NodeStatus.SUCCESS,
                    data={"verdict": verdict},
                )

        except Exception as e:
            # If reflection fails, assume OK (don't block on reflection)
            logger.warning("Reflection LLM call failed; assuming OK: %s", e)
            return NodeResult(
                node_name=self.name,
                status=NodeStatus.SUCCESS,
                data={"verdict": "OK", "reflect_error": str(e)},
            )

    def _build_reflect_prompt(
        self,
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
