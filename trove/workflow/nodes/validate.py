"""Deterministic validation node — rule checks between execute and reflect.

Rule failures use the same error_feedback correction channel as
execution errors (shared retry budget): the regenerated SQL gets the
concrete rule reason in its prompt. This is the code-side counterpart
to the LLM reflect judge — what can be checked deterministically
should not be left to the model.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.i18n import L
from trove.workflow.nodes.planner import answer_columns_mismatch, extra_columns_mismatch
from trove.workflow.rules import verify as run_rules
from trove.workflow.state import WorkflowState, budget_exhausted


def make_validate_rules(
    max_retries: int = 10,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the validate node.

    Args:
        max_retries: Shared correction budget — rule failures feed back
            to gen_sql while retry_count < max_retries; once exhausted,
            failures degrade gracefully via state.error.

    The verify_step assertion layer reports structured hits
    (rule name + reason) via state.validation_hits for eval attribution.
    """

    async def validate(state: WorkflowState) -> dict[str, Any]:
        # Upstream failure / pending execution feedback — pass through
        if state.error or state.error_feedback:
            return {}

        reason, hits = run_rules(
            state.question, state.sql, state.columns, state.rows, state.row_count,
            lang=state.lang,
        )
        if reason is not None:
            if budget_exhausted(state.retry_count, max_retries):
                return {"error": reason}
            feedback = L(
                state.lang,
                f"校验规则: {reason}",
                f"Validation rule: {reason}",
            )
            return {
                "error_feedback": feedback,
                "retry_count": state.retry_count + 1,
                "correction_history": [feedback],
                "validation_hits": hits,
            }

        # 层2:answer_columns 与执行结果列的一致性检查(plan 的钦点列必须
        # 能在结果里看到;全部缺失才是冲突——别名/表达式会制造单列噪音,
        # 全缺才是 SELECT 列表整体背离计划的强信号)。走 analyze_error
        # 通道,反馈文本把归因指向 planner 的 answer_columns。
        ac_errors = answer_columns_mismatch(state.plan_json, state.columns)
        if ac_errors:
            if budget_exhausted(state.retry_count, max_retries):
                return {"error": "; ".join(ac_errors)}
            feedback = L(
                state.lang,
                f"计划校验: {'; '.join(ac_errors)}。"
                "查询计划的 answer_columns 与执行结果列不符——重新规划并修正输出列。",
                f"Plan check: {'; '.join(ac_errors)}. "
                "The query plan's answer_columns do not match the executed "
                "result columns — re-plan and fix the answer columns.",
            )
            return {
                "error_feedback": feedback,
                "retry_count": state.retry_count + 1,
                "correction_history": [feedback],
                "validation_hits": [{
                    "rule": "answer-columns",
                    "reason": "; ".join(ac_errors),
                }],
            }

        # 层2补充:结果列"多余"检查(plan 的 answer_columns 必须覆盖结果列;
        # 结果多出计划外的列 → 打回重规划)。保守:全部 refs 都在结果里才
        # 判定;question 点名列豁免——宁漏勿误,误伤成本=一次重试轮。
        extra_errors = extra_columns_mismatch(
            state.plan_json, state.columns, state.question,
        )
        if extra_errors:
            if budget_exhausted(state.retry_count, max_retries):
                return {"error": "; ".join(extra_errors)}
            feedback = L(
                state.lang,
                f"计划校验: {'; '.join(extra_errors)}。"
                "只输出查询计划的 answer_columns——去掉多余列。",
                f"Plan check: {'; '.join(extra_errors)}. "
                "Output only the plan's answer_columns — drop the extra columns.",
            )
            return {
                "error_feedback": feedback,
                "retry_count": state.retry_count + 1,
                "correction_history": [feedback],
                "validation_hits": [{
                    "rule": "extra-columns",
                    "reason": "; ".join(extra_errors),
                }],
            }
        return {}

    return validate
