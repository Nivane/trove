"""Error analysis node — classify, judge, and plan the fix for a failed SQL.

Runs before regeneration when a correction is pending: the LLM
classifies the error type (Schema Linking / Join / aggregation /
filter / business semantics), explains what is wrong with the failed
SQL, and proposes a concrete fix. The analysis is injected into the
regeneration prompt and shown in the trajectory.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.i18n import L, detect_language
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

ANALYZE_ZH = """你是 SQL 错误诊断专家。给定失败的 SQL、错误信息、问题与相关表结构，输出三部分：

1. 错误类型：Schema Linking / Join / 聚合 / 过滤 / 业务语义（选一）
2. 判断：这条 SQL 错在哪里，为什么会错
3. 修正方案：具体怎么改

简洁输出，不要输出完整 SQL。"""

ANALYZE_EN = """You are a SQL error diagnosis expert. Given a failed SQL, the error, the question, and relevant schema, output three parts:

1. Error type: Schema Linking / Join / Aggregation / Filter / Business semantics (pick one)
2. Judgment: what is wrong with this SQL and why
3. Fix plan: how to correct it concretely

Be concise; do not output the full SQL."""


def make_analyze_error(
    llm: LLMGateway,
    config: AgentConfig,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    async def analyze_error(state: WorkflowState) -> dict[str, Any]:
        if not state.error_feedback or state.error:
            return {}

        try:
            model = config.target or "openai/gpt-4o"
            system_prompt = L(
                detect_language(state.question),
                ANALYZE_ZH,
                ANALYZE_EN,
            )
            prompt = (
                f"Question: {state.question}\n"
                f"Failed SQL: {state.sql}\n"
                f"Error: {state.error_feedback}\n"
                f"Schema context: {state.schema_context[:1200]}\n"
            )
            analysis = await llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=300,
                metadata={
                    "node": "analyze_error",
                    "session_id": state.session_id,
                    "run_id": state.run_id,
                    "question": state.question[:80],
                },
            )
            analysis = analysis.strip()
            return {"error_analysis": analysis} if analysis else {}
        except Exception as e:
            logger.warning("Error analysis failed (proceeding with raw feedback): %s", e)
            return {}

    return analyze_error
