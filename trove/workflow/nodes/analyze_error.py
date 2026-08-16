"""Error analysis node — diagnose failures AND decide the rollback target.

Runs on every correction (execution error / rule failure / consensus
disagreement / reflect RETRY): the LLM classifies the error, explains
what is wrong, proposes a fix, and picks which upstream step to roll
back to ("TARGET: gen_sql|planner|schema_linking"). The decision is
enforced deterministically by the rollback ladder: a repeated target
escalates one rung (anti-loop guard), and repeating the top rung
degrades gracefully — the shared retry budget still guarantees
termination.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.i18n import L
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

ANALYZE_ZH = """你是 SQL 错误诊断专家。给定失败的 SQL、错误信息、问题与相关表结构，输出四部分：

1. 错误类型：Schema Linking / Join / 聚合 / 过滤 / 业务语义（选一）
2. 判断：这条 SQL 错在哪里，为什么会错
3. 修正方案：具体怎么改
4. 回退目标：判断失败根因在哪一步，输出一行 "TARGET: gen_sql|planner|schema_linking"
   （SQL 本身写错→gen_sql；查询计划/聚合思路错→planner；表/列匹配错或漏表→schema_linking；无法判断时→gen_sql）

如果给出了 Evidence（官方提示），它代表该题的标准业务语义；当它与你的领域知识冲突时，以 Evidence 为准。

如果问题本身不是数据查询（表含义/术语定义/知识性问题），只输出一行："NO_SQL: <一句话>"，不要输出错误诊断。

简洁输出，不要输出完整 SQL。"""

ANALYZE_EN = """You are a SQL error diagnosis expert. Given a failed SQL, the error, the question, and relevant schema, output four parts:

1. Error type: Schema Linking / Join / Aggregation / Filter / Business semantics (pick one)
2. Judgment: what is wrong with this SQL and why
3. Fix plan: how to correct it concretely
4. Rollback target: decide which step is at fault and output one line "TARGET: gen_sql|planner|schema_linking"
   (SQL itself wrong → gen_sql; plan/aggregation logic wrong → planner; table/column matching wrong or missing → schema_linking; unsure → gen_sql)

If Evidence (an official hint) is given, it states the question's standard business semantics; when it conflicts with your domain knowledge, trust the Evidence.

If the question itself is not answerable by SQL (table meaning / term definition / knowledge question), output ONLY one line: "NO_SQL: <one sentence>", nothing else.

Be concise; do not output the full SQL."""

ROLLBACK_TARGET_RE = re.compile(r"TARGET\s*[:：]\s*(\w+)", re.I)
DEFAULT_ROLLBACK_LADDER = ("gen_sql", "planner", "schema_linking")


def render_reasoning_context(
    history: list[dict[str, str]],
    nodes: tuple[str, ...] = ("gen_sql", "planner"),
    limit: int = 2,
    width: int = 600,
) -> str:
    """从思考痕迹历史里挑最近 N 条指定节点的轨迹,拼成回退上下文。

    Args:
        history: state.reasoning_history(operator.add 累积的 {node, text})。
        nodes: 只取这些节点产生的痕迹。
        limit: 最多取最近几条。
        width: 每条痕迹的字符上限。

    Returns:
        空串(无痕迹)或 "[node] text" 行的拼接。
    """
    picked = [h for h in history if h.get("node") in nodes][-limit:]
    parts = []
    for h in picked:
        text = (h.get("text") or "")[:width]
        if text:
            parts.append(f"[{h['node']}] {text}")
    return "\n".join(parts)


def _resolve_rollback(parsed: str, ladder: list[str], last: str) -> str | None:
    """Anti-loop guard: a repeated target escalates one rung up the ladder.

    Returns:
        The resolved target, or None when the top rung repeats (→ degrade).
    """
    target = parsed if parsed in ladder else ladder[0]
    if target != last:
        return target
    idx = ladder.index(target)
    if idx + 1 < len(ladder):
        return ladder[idx + 1]
    return None


def make_analyze_error(
    llm: LLMGateway,
    config: AgentConfig,
    rollback_ladder: tuple[str, ...] = DEFAULT_ROLLBACK_LADDER,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the diagnose-and-decide node bound to an LLM gateway.

    Args:
        rollback_ladder: Ordered rollback targets available in the graph
            (e.g. without the planner node, planner is absent from the
            ladder and can never be picked).
    """
    ladder = list(rollback_ladder)

    async def analyze_error(state: WorkflowState) -> dict[str, Any]:
        if state.error:
            return {}
        # Runs on execution/rule/consensus failures (error_feedback) and
        # on reflect RETRY verdicts (reason carries the failure context).
        if not state.error_feedback and state.verdict != "RETRY":
            return {}

        try:
            model = config.target or "openai/gpt-4o"
            system_prompt = L(
                state.lang,
                ANALYZE_ZH,
                ANALYZE_EN,
            )
            error_text = state.error_feedback or state.reason
            prompt = (
                f"Question: {state.question}\n"
                f"Failed SQL: {state.sql}\n"
                f"Error: {error_text}\n"
                f"Schema context: {state.schema_context[:1200]}\n"
            )
            # 官方提示是业务语义的权威锚点:诊断不得把 evidence 支持的解释判为错误
            if state.evidence:
                prompt += (
                    f"Evidence (official hint, authoritative): {state.evidence}\n"
                )
            # 上一轮生成/规划方的思考痕迹:定位误判根因的关键上下文
            trail = render_reasoning_context(state.reasoning_history)
            if trail:
                prompt += (
                    "Prior reasoning trail (thinking/tool trace from the "
                    f"failed generation):\n{trail}\n"
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
            if analysis.upper().startswith("NO_SQL"):
                # The question itself is not a SQL question — route to the
                # metadata answer path. Clear error_feedback so the stale
                # correction note is not injected into the answer prompt
                # (and metadata_check actually runs).
                return {"no_sql": True, "error_feedback": "", "error_analysis": analysis}
            if not analysis:
                return {}

            match = ROLLBACK_TARGET_RE.search(analysis)
            parsed = match.group(1).lower() if match else ""
            target = _resolve_rollback(parsed, ladder, state.last_rollback_target)
            if target is None:
                return {
                    "error": (
                        f"回退目标 {parsed or 'gen_sql'} 连续失败且无档可升，优雅降级"
                    ),
                }
            return {
                "error_analysis": analysis,
                "rollback_target": target,
                "last_rollback_target": target,
            }
        except Exception as e:
            logger.warning("Error analysis failed (proceeding with raw feedback): %s", e)
            return {}

    return analyze_error
