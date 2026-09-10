"""HITL node — pauses before execution for human confirmation.

Uses LangGraph's native ``interrupt()`` to gate on the user's final
query before it is executed against the datasource.

Behavior:
  - When ``config.hitl`` is disabled, no SQL present, an upstream error
    is set, or this is a correction round (retry in progress), the node
    passes through untouched — no pause.
  - Otherwise it interrupts with a confirmation request (question, SQL,
    semantic explanation). On resume:
      - approved (yes/approve/ok/confirm) → ``hitl_status="approved"``,
        the graph continues to execute_sql.
      - rejected (no/reject/cancel) → ``hitl_status="rejected"`` and an
        abort message; the graph routes to output without executing.

The gate fires on the *first* pass (initial final query) and is a no-op
on correction rounds, so a human confirms once, before the first run.

Requires a checkpointer for the interrupt to persist across resumes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

_APPROVE = {"approve", "yes", "y", "ok", "confirm", "1", "true"}
_APPROVE_ALL = {"approve_all", "approveall", "ya", "2"}
_REJECT = {"reject", "no", "n", "cancel", "0", "false"}


def _normalize(decision: Any) -> str:
    """任意 resume 载荷 → "approved" / "rejected" / "unrecognized"。

    未识别的载荷一律 **不批准**:HITL 是执行前的安全门,失手放行(执行了没人
    确认过的 SQL)与失手拒绝(请用户再说一次)代价不对称。此前未识别载荷
    (``{}``、``null``、未知字符串、数字)全部落到 ``approved``,任何畸形
    resume 请求都能静默穿过人工确认门。
    """
    if isinstance(decision, bool):
        return "approved" if decision else "rejected"
    if isinstance(decision, dict):
        decision = decision.get("decision", decision.get("approve"))
    if isinstance(decision, bool):
        return "approved" if decision else "rejected"
    if isinstance(decision, str):
        d = decision.strip().lower()
        if d in _APPROVE or d in _APPROVE_ALL:
            return "approved"  # approve_all also approves the current task
        if d in _REJECT:
            return "rejected"
    return "unrecognized"


def make_hitl(
    config: AgentConfig,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the HITL gate bound to config.

    Returns:
        Async node function taking WorkflowState and returning a partial update.
    """

    async def hitl(state: WorkflowState) -> dict[str, Any]:
        # No gate when disabled, no SQL, errored upstream, or mid-correction
        # (retry loop → regenerating anyway; do not re-prompt the human).
        # 批内"确认并继续全部":后续子任务不再暂停,直接放行
        # (hitl_status=approved 保持结果口径一致,避免下游误判未确认)
        if state.auto_approve:
            return {"hitl_status": "approved"}

        in_correction = bool(state.error_feedback or state.error_analysis or state.reason)
        if (
            not config.hitl
            or not state.sql
            or state.error
            or in_correction
        ):
            return {}

        proposal = {
            "kind": "confirm_sql",
            "question": state.question,
            "sql": state.sql,
            "semantics": state.semantics,
            "task_context": state.task_context,
        }
        try:
            decision = interrupt(proposal)
        except GraphInterrupt:
            # interrupt() 抛 GraphInterrupt 是正常的暂停信号 —— 必须放行,
            # 由 LangGraph 挂起图,等待外部 resume。绝不能在此吞掉。
            raise
        except Exception as e:
            # 其他失败(如缺 checkpointer)不应阻断执行。
            logger.warning("HITL interrupt unavailable (%s); proceeding without gate", e)
            return {"hitl_status": "approved"}

        status = _normalize(decision)
        if status == "rejected":
            return {
                "hitl_status": "rejected",
                "intent_answer": (
                    "已取消该查询的执行(人工否决 SQL)。"
                    if state.lang == "zh"
                    else "Query execution cancelled (SQL rejected by user)."
                ),
            }
        if status == "unrecognized":
            # 未获确认 → 不执行。走与显式否决相同的取消路由
            # (_route_after_hitl 按 hitl_status == "rejected" 分流),但如实
            # 说明是「确认指令没被识别」,不谎称用户否决过。
            logger.warning(
                "HITL resume payload unrecognized (%r); not executing SQL", decision)
            return {
                "hitl_status": "rejected",
                "intent_answer": (
                    "未识别到确认指令,已放弃执行该查询。请重新确认后再试。"
                    if state.lang == "zh"
                    else "Unrecognized confirmation; the query was not executed. "
                         "Please confirm again."
                ),
            }
        return {"hitl_status": "approved"}

    return hitl
