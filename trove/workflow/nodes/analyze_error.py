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
from trove.prompts import render
from trove.prompts.skills import render_skills
from trove.workflow.state import WorkflowState
from trove.workflow.versions import (
    EXEC_FAILURE_SIG,
    extract_rule_hits,
    record_version,
    regression_report,
    regression_state,
    result_sig,
)

logger = get_logger(__name__)

ROLLBACK_TARGET_RE = re.compile(r"TARGET\s*[:：]\s*(\w+)", re.I)
DEFAULT_ROLLBACK_LADDER = ("gen_sql", "planner", "schema_linking")

# 语义级规则族:过滤条件缺失/错误是「问题意图没被理解」→ revisor
# （其余规则族 F1 形状 / F3 值域 / F4 排序都是实现形态 → fixer）
REVISOR_RULE_GROUPS = {"F2"}
# 投票平局文本:候选解释不一致 → 语义重估
_REVISOR_TEXTS = ("differ", "disagree", "不一致", "分歧")

# 连续无进展轮数上限:达到后停止迭代打回（省预算 + 明确降级信号）
MAX_NO_PROGRESS_ROUNDS = 3


def classify_fix_mode(error_text: str, issues: list[str]) -> str:
    """失败 → 修复模式: fixer（实现级定点修）| revisor（语义重写）。

    确定性判定,零额外 LLM 输出（修复模式与诊断文本同时可得）:
      1. 规则命中（issues）优先: F2 族（过滤）→ revisor,其余 → fixer
      2. 无规则命中: 投票平局文本 → revisor;其余（执行/语法错误等）→ fixer
      3. 默认 fixer: 未知失败先做最小实现级修复,回归检查负责纠偏
    """
    if issues:
        groups = {i.split("-", 1)[0] for i in issues}
        return "revisor" if groups & REVISOR_RULE_GROUPS else "fixer"
    low = (error_text or "").lower()
    if any(k in low for k in _REVISOR_TEXTS):
        return "revisor"
    return "fixer"


def _fallback_analysis(error_text: str, lang: str) -> str:
    """LLM 诊断空输出时的确定性兜底:按失败类型给出可执行检查项。

    修正循环里诊断为空 = 生成方下一轮只看到原始错误,容易反复产出
    同一错误解释(实测:0 行列表问题 10 轮重试全部重蹈覆辙)。兜底
    诊断按错误文本的模式给出最可能的修正方向。
    """
    low = (error_text or "").lower()
    if "no rows" in low or "zero rows" in low or "零行" in low:
        return L(
            lang,
            "空结果通常意味着过滤条件过严或 join 键错误。逐个检查 WHERE 条件:"
            "与问题关系最弱的条件先放宽或去掉;同时核对 join 列是否用对。",
            "An empty result usually means a filter is too strict or a join key is "
            "wrong. Re-check every WHERE condition; relax or drop the least certain "
            "one first, and verify the join columns are correct.",
        )
    if "syntax" in low or "1064" in low:
        return L(
            lang,
            "语法错误。简化写法:避免 CTE/UNION/复杂嵌套,改用直白的 SELECT+JOIN。",
            "Syntax error. Simplify the SQL: avoid CTEs, UNION, or deep nesting; "
            "prefer a plain SELECT with JOINs.",
        )
    if "timed out" in low or "timeout" in low:
        return L(
            lang,
            "查询超时。减少参与的表或缩小过滤范围,避免笛卡尔积。",
            "Query timed out. Reduce the number of tables joined or narrow the "
            "filters; avoid cartesian products.",
        )
    if "differ" in low or "不一致" in low:
        return L(
            lang,
            "多个候选结果不一致。选择与问题措辞最吻合的解释,重新生成。",
            "Candidate results disagree. Pick the interpretation that best matches "
            "the question wording and regenerate.",
        )
    return L(
        lang,
        "上一次查询未达到要求。重新对照问题的每个条件,逐一检查 SQL 的 "
        "WHERE/JOIN/聚合是否正确。",
        "The previous query did not meet requirements. Re-check each condition of "
        "the question against the SQL's WHERE/JOIN/aggregation.",
    )


def _hypothesis_fingerprint(sql: str) -> str:
    """失败 SQL 的归一化指纹(折叠空白 + 小写)——去重口径。"""
    return " ".join((sql or "").split()).lower()


def record_rejected_hypothesis(
    existing: list[dict[str, str]],
    sql: str,
    reason: str,
    sql_limit: int = 160,
    reason_limit: int = 220,
) -> list[dict[str, str]]:
    """把本轮失败假设(错误 SQL + 原因)记入黑名单,指纹去重,摘要限长。

    Returns:
        需要追加的假设列表(已在黑名单 → 空列表,不重复累积)。
    """
    if not sql:
        return []
    fp = _hypothesis_fingerprint(sql)
    if any(_hypothesis_fingerprint(h.get("sql", "")) == fp for h in existing):
        return []
    return [{
        "sql": " ".join(sql.split())[:sql_limit],
        "reason": (reason or "")[:reason_limit],
    }]


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


def _resolve_rollback(
    parsed: str, ladder: list[str], last: str, same_failure: bool,
) -> str | None:
    """Anti-loop guard: a repeated target escalates one rung up the ladder.

    Escalation only fires when the SAME failure repeats (identical raw
    error text as the previous recorded round). Different failure causes
    independently picking the same target is a fresh judgment, not a loop
    — e.g. an execution error followed by a semantic RETRY both targeting
    gen_sql must NOT escalate (the second round is a different problem).

    Returns:
        The resolved target, or None when the top rung repeats (→ degrade).
    """
    target = parsed if parsed in ladder else ladder[0]
    if target != last or not same_failure:
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
            system_prompt = render("analyze_error/system", lang=state.lang)
            # 方法论 skill:按节点确定性匹配(manifest.yml),注入 system prompt
            skill_block = render_skills("analyze_error", lang=state.lang)
            if skill_block:
                system_prompt = f"{system_prompt}\n\n{skill_block}"
            raw_error = state.error_feedback or state.reason
            # 版本链回归检查:对比上一版失败(签名/规则命中),产出确定性反馈
            # 并入诊断输入——模型必须看到「无效修复/无进展/问题转移」
            issues = extract_rule_hits(raw_error)
            prev = state.sql_versions[-1] if state.sql_versions else None
            # 执行错误(本轮 SQL 未执行 → row_count == -1):rows 为空或
            # 上一轮成功的残留,结果集签名无意义——两轮不同错误也会
            # 签名相同被误判"无效修复"。改用原始错误文本判定同一失败
            # 是否重演(引擎错误文本是确定性的,同一错误文本 = 同一失败)。
            exec_failed = state.row_count == -1
            same_failure = bool(prev) and bool(raw_error) and prev.get("error") == raw_error
            if exec_failed:
                report = (
                    f"Invalid fix: the same execution error as Round {prev['round']} "
                    "(identical error message — do not repeat the same SQL)."
                    if same_failure else None
                )
                progress = "invalid" if same_failure else (
                    "first" if prev is None else "improved"
                )
            else:
                report = regression_report(prev, result_sig(state.rows), issues)
                progress = regression_state(prev, result_sig(state.rows), issues)
            error_text = raw_error
            if report:
                error_text = f"{raw_error}\n[Regression check] {report}"
            # 上一轮生成/规划方的思考痕迹:定位误判根因的关键上下文
            trail = render_reasoning_context(state.reasoning_history)
            prompt = render(
                "analyze_error/user",
                question=state.question,
                sql=state.sql,
                error=error_text,
                schema_context=state.schema_context[:10000],
                evidence=state.evidence,
                trail=trail,
            )
            analysis = await llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                # 推理模型 reasoning 占用预算,小预算会导致诊断文本被截断/为空;
                # 统一放宽输出上限(见 gateway 默认值)
                max_tokens=16000,
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
                # LLM 调用成功但输出为空:确定性兜底诊断替代静默放行,
                # 生成方下一轮仍能得到可执行的修正方向(而非空诊断)。
                analysis = _fallback_analysis(error_text, state.lang)

            match = ROLLBACK_TARGET_RE.search(analysis)
            parsed = match.group(1).lower() if match else ""
            target = _resolve_rollback(
                parsed, ladder, state.last_rollback_target, same_failure,
            )
            if target is None:
                return {
                    "error": (
                        f"回退目标 {parsed or 'gen_sql'} 连续失败且无档可升，优雅降级"
                    ),
                }
            # 缺口3: 修复模式判定（fixer 实现级 vs revisor 语义级),注入重生成方
            fix_mode = classify_fix_mode(error_text, issues)
            # 缺口5: 修复进展量化 —— regression_state 标签 + 无进展轮计数
            no_progress = (
                0 if progress in ("first", "improved")
                else state.no_progress_rounds + 1
            )
            if no_progress >= MAX_NO_PROGRESS_ROUNDS:
                # 连续无进展:打回重生成已无意义(结果未变/问题维度未变),
                # 提前停止迭代省预算,输出方走优雅降级路径。诊断数据仍随行,
                # 供日志归因「为什么停止迭代」。
                return {
                    "error": (
                        f"连续 {MAX_NO_PROGRESS_ROUNDS} 轮修复无进展"
                        f"({progress}),停止迭代,优雅降级"
                    ),
                    "last_progress": progress,
                    "no_progress_rounds": no_progress,
                }
            return {
                "error_analysis": analysis,
                "rollback_target": target,
                "last_rollback_target": target,
                "fix_mode": fix_mode,
                "last_progress": progress,
                "no_progress_rounds": no_progress,
                "rejected_hypotheses": record_rejected_hypothesis(
                    state.rejected_hypotheses, state.sql, error_text,
                ),
                # 版本链:记录本轮失败版本供下一轮定点修复。执行错误
                # 用哨兵签名(rows 无意义),错误文本随之记录供"同一失败
                # 重演"判定与提示注入。
                "sql_versions": record_version(
                    state.sql_versions, state.sql,
                    EXEC_FAILURE_SIG if exec_failed else result_sig(state.rows),
                    issues,
                    round_n=len(state.sql_versions) + 1,
                    error=raw_error,
                ),
            }
        except Exception as e:
            logger.warning("Error analysis failed (proceeding with raw feedback): %s", e)
            return {}

    return analyze_error
