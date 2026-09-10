"""Deterministic validation node — rule checks between execute and reflect.

Rule failures use the same error_feedback correction channel as
execution errors (shared retry budget): the regenerated SQL gets the
concrete rule reason in its prompt. This is the code-side counterpart
to the LLM reflect judge — what can be checked deterministically
should not be left to the model.

层2 的列检查以 query_sketch 的结构化计划为输入,所以它**可能没有输入**:
散文计划(模型没按 JSON 输出)下 ``plan_json is None``,两个检查函数一律返回
``[]``。此时本节点写 ``plan_validation.status = "untyped"`` 并记日志 —— 降级
可以说,但不能不说(A1-3)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.i18n import L
from trove.core.logging import get_logger
from trove.llm.observability import record_span
from trove.workflow.nodes.query_sketch import answer_columns_mismatch, extra_columns_mismatch
from trove.workflow.rules import verify as run_rules
from trove.workflow.state import WorkflowState, budget_exhausted

logger = get_logger(__name__)


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

        # 规则链结果进 langfuse(hits = 规则名+原因,即修正指令的证据)
        with record_span(
            "rules.verify",
            input={"question": state.question, "sql": state.sql},
        ) as span:
            reason, hits = run_rules(
                state.question, state.sql, state.columns, state.rows, state.row_count,
                lang=state.lang,
            )
            if span is not None:
                span.update(output={"passed": reason is None, "failures": hits})
        if reason is not None:
            if budget_exhausted(state.retry_count, max_retries):
                return {"error": reason, "rules_passed": False}
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
                "rules_passed": False,
            }

        # 层2:answer_columns 与执行结果列的一致性检查(plan 的钦点列必须
        # 能在结果里看到;全部缺失才是冲突——别名/表达式会制造单列噪音,
        # 全缺才是 SELECT 列表整体背离计划的强信号)。走 analyze_error
        # 通道,反馈文本把归因指向 query_sketch 的 answer_columns。
        ac_errors = answer_columns_mismatch(state.plan_json, state.columns)
        if ac_errors:
            if budget_exhausted(state.retry_count, max_retries):
                return {"error": "; ".join(ac_errors), "rules_passed": False}
            feedback = L(
                state.lang,
                f"计划校验: {'; '.join(ac_errors)}。"
                "查询计划的 answer_columns 与执行结果列不符——重新规划并修正输出列。",
                f"Plan check [answer-columns]: {'; '.join(ac_errors)}. "
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
                "rules_passed": False,
            }

        # 层2补充:结果列"多余"检查(plan 的 answer_columns 必须覆盖结果列;
        # 结果多出计划外的列 → 打回重规划)。保守:全部 refs 都在结果里才
        # 判定;question 点名列豁免——宁漏勿误,误伤成本=一次重试轮。
        extra_errors = extra_columns_mismatch(
            state.plan_json, state.columns, state.question, state.sql,
        )
        if extra_errors:
            if budget_exhausted(state.retry_count, max_retries):
                return {"error": "; ".join(extra_errors), "rules_passed": False}
            feedback = L(
                state.lang,
                f"计划校验: {'; '.join(extra_errors)}。"
                "只输出查询计划的 answer_columns——去掉多余列。",
                f"Plan check [extra-columns]: {'; '.join(extra_errors)}. "
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
                "rules_passed": False,
            }
        # 列检查"本该跑却跑不了"——必须说出来。
        #
        # answer_columns_mismatch / extra_columns_mismatch 都以 ``plan_json``
        # 为输入,而它在散文计划(query_sketch 没按 JSON 输出)下是 None;两个
        # 函数对 None 一律返回 [],于是**整层列守卫静默消失**。回答照常交付,
        # 只是少了那道闸,而且外部看不出来。
        #
        # 判据用 ``columns`` 而不是"有没有 plan":没有结果列就无所谓列检查
        # (快径/未启用 query_sketch 的路径都落在这一侧),不该被记一笔。
        # 注意 ``compiled=True`` 不会走到这里——它只在 plan_json 是 dict 时置位
        # (query_sketch.py),那条路由契约负责校验。
        #
        # 状态写进已有的 ``plan_validation`` 通道:query_sketch 用同一字段报
        # "dropped"(plan 没过 schema 校验,此时 plan 被清空、列检查同样无从跑)
        # / "ok"。**不写 validation_hits** —— 那个通道的语义是"被规则拦过",
        # 是 eval 恢复机制归因的判据(trove/eval/replay.py::_tried_recovery),
        # 混进非拦截事件会污染归因。
        if state.plan_json is None and state.columns:
            logger.info(
                "column guards skipped (no typed plan) for %r",
                state.question[:80],
            )
            return {
                "rules_passed": True,
                "plan_validation": {
                    "status": "untyped",
                    "reason": "no typed plan — answer/extra column checks skipped",
                },
            }

        # 全过:确定性规则链 + 层2 计划检查全通过 → 显式正向信号,
        # reflect 据此(配合复杂度)决定是否跳过 LLM 裁决。
        return {"rules_passed": True}

    return validate
