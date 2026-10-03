"""Metadata answer validation — hallucination rules + LLM judge.

The LLM-composed metadata answer gets the same treatment as the SQL
pipeline: deterministic checks first (referenced table.column must
exist), then an LLM judge (complete answer? no fabrication?). Failures
feed back to answer_metadata through the shared error_feedback
channel with a concrete reason.

Termination contract (mirrors execute_sql's):
- the feedback **consumer** clears it — answer_metadata returns
  ``error_feedback: ""`` on every delivered answer, so a pending
  feedback means "a retry is in flight", never "the last verdict";
- the **budget** lives here: every failing verdict increments
  retry_count, and at the cap the node escalates to ``state.error``.
  The graph routes that error straight to output — the one shape that
  is a loop is error → answer_metadata, because that path has neither
  the counter nor the clearer.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.prompts import render
from trove.services.datasource.registry import ConnectorRegistry
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

_REF_RE = re.compile(r"\b([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)\b")

def find_hallucinations(answer: str, table_columns: dict[str, list[str]]) -> list[str]:
    """table.column references that do not exist in the schema."""
    hallucinations = []
    for m in _REF_RE.finditer(answer):
        table, column = m.group(1), m.group(2)
        if table not in table_columns or column not in table_columns[table]:
            hallucinations.append(f"{table}.{column}")
    return hallucinations


def make_metadata_check(
    connectors: ConnectorRegistry | None = None,
    llm: LLMGateway | None = None,
    config: AgentConfig | None = None,
    max_retries: int = 10,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    async def metadata_check(state: WorkflowState) -> dict[str, Any]:
        # 只对 error 透传。**不**对在途 error_feedback 短路(validate 那样做
        # 是因为它的预算在失败生产者 execute_sql 手里);本节点自己就是预算
        # 持有者,跳过 = retry_count 冻结、max_retries 永不可达,裁决也永远
        # 停在第一版答案上 —— 实测循环只能靠 langgraph 递归上限兜底。
        if state.error:
            return {}

        # 1. Deterministic hallucination check
        if connectors is not None:
            try:
                schema = await connectors.get_schema(state.datasource or None)
                table_columns = {t.name: [c.name for c in t.columns] for t in schema.tables}
                hallucinations = find_hallucinations(state.intent_answer, table_columns)
                if hallucinations:
                    feedback = (
                        f"答案引用了不存在的信息: {', '.join(hallucinations[:3])}。"
                        f"请只依据提供的元数据重新回答。"
                    )
                    if state.retry_count >= max_retries:
                        return {"error": feedback}
                    return {
                        "error_feedback": feedback,
                        "retry_count": state.retry_count + 1,
                        "correction_history": [feedback],
                    }
            except Exception as e:
                logger.debug("Metadata hallucination check failed: %s", e)

        # 2. LLM judge
        if llm is not None:
            try:
                model = (config.target if config else "") or "openai/gpt-4o"
                system_prompt = render("metadata_check/system", lang=state.lang)
                verdict = await llm.chat(
                    model=model,
                    max_tokens=16000,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": render(
                            "metadata_check/user",
                            # 评审必须对照**答案生成时看到的那份**上下文
                            # (answer_metadata 的快照):重算既是重复的目录/
                            # 血缘读,也可能与生成时不一致;system 里"只引用
                            # 上下文中存在的信息"这条判据没有它无法裁决。
                            context=state.metadata_context,
                            question=state.question,
                            answer=state.intent_answer,
                        )},
                    ],
                    metadata={
                        "node": "metadata_check",
                        "session_id": state.session_id,
                        "run_id": state.run_id,
                        "question": state.question[:80],
                    },
                )
                verdict = verdict.strip().upper()
                if verdict.startswith("ISSUE"):
                    reason = verdict.replace("ISSUE", "", 1).strip(":： ")
                    feedback = f"答案存在问题：{reason or '未完整回答'}。请修正后重新回答。"
                    if state.retry_count >= max_retries:
                        return {"error": feedback}
                    return {
                        "error_feedback": feedback,
                        "retry_count": state.retry_count + 1,
                        "correction_history": [feedback],
                    }
            except Exception as e:
                logger.warning("Metadata judge failed (passing): %s", e)

        # 通过即显式清反馈:在途反馈 = 路由信号(按真值判定下一跳)。判过、
        # 通过 = 这一轮没意见,信号必须灭 —— 否则路由拿着上一轮的火种回头
        # 重答,答案再新也验证不到(消费者已清的情况下这里是幂等的 no-op)。
        return {"error_feedback": ""}

    return metadata_check
