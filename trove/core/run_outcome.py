"""一次运行「怎么结束」的唯一词汇表（运行轴）。

与另外两条轴刻意分开：

* **覆盖率轴**（``trove/services/question_coverage/taxonomy.py`` 的 10 结局）
  描述批量回放里编译/执行的覆盖程度，不是一次交互运行的结局；
* **域内状态**（verdict / effect / delivery …）是各自领域的事实，不在这里
  重命名。

**降级不是结局**：一次运行可以在 ``answered`` 的同时带降级标记（分析降级、
噪声带判不了、``outside_band=None``），标记必须留在各自的载荷里可见——
把它折叠成第七种结局，会让「回答了什么」与「哪一步没做成」混成一个字段。

消费者：会话 summary（SSE ``done`` / 历史回放）、运行记录 finish、MCP
``ask_data``。本模块零 ``trove.*`` import、零 I/O、不读墙钟——tracing/MCP
直接用它不会制造导入环。
"""
from __future__ import annotations

from typing import Any, Mapping

#: 运行结局闭集。增删都是契约变更（``tests/architecture/test_outcome_contracts.py``）。
RUN_OUTCOMES: tuple[str, ...] = (
    "answered",
    "refused",
    "clarified",
    "pending_approval",
    "cancelled",
    "failed",
)

#: 运行记录的生命周期状态 → 结局。``done`` 是生命周期终点而不是结局：
#: 拒绝轮与澄清轮都是 ``done``，它们各自结束成什么只能由 finish summary
#: 自己的 ``outcome`` 说话。
RECORD_STATUS_OUTCOME: Mapping[str, str | None] = {
    "running": None,
    "pending_hitl": "pending_approval",
    "done": None,
    "failed": "failed",
    "error": "failed",
    "cancelled": "cancelled",
}


def classify_run(
    *,
    error: str = "",
    refusal: Any = None,
    clarification_question: str = "",
    hitl_status: str = "",
) -> str:
    """从 final state 的信号判结局；优先级与投递/生命周期一致。

    顺序是承重的：人工确认前的暂停先于一切（那一刻没有答案可言）；错误
    先于拒绝/澄清（一次没跑完的运行不能报成一次「正常的拒绝」）；拒绝
    先于澄清，因为拒绝轮**也**写 ``clarification_question``。
    未知的 ``hitl_status`` 不抛错——分类器绝不能成为一次运行失败的原因。
    """
    if hitl_status == "pending":
        return "pending_approval"
    if hitl_status == "rejected":
        return "cancelled"
    if error:
        return "failed"
    if refusal:
        return "refused"
    if clarification_question:
        return "clarified"
    return "answered"


def record_outcome(
    status: str, finish: Mapping[str, Any] | None = None,
) -> str | None:
    """运行记录的结局：finish summary 说了算，状态只补没有 finish 的那些。

    ``done`` 的记录读 ``finish_json["outcome"]``（拒绝/澄清也是 done）；
    ``running`` 尚未结束、``done`` 却没有 finish 时无结局可读，返回
    ``None``——不拿生命周期状态冒充结局。未知状态抛错：闭集不许静默兜底。
    """
    if status not in RECORD_STATUS_OUTCOME:
        raise ValueError(f"unknown run record status: {status!r}")
    value = (finish or {}).get("outcome")
    if value in RUN_OUTCOMES:
        return value
    return RECORD_STATUS_OUTCOME[status]
