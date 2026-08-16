"""Reflect node — evaluates query result quality.

Division of labor (Sentinel-style deterministic verification):
- structural checks (row shape, dtype, value range) are DETERMINISTIC
  rules in trove.workflow.rules, run before this node; failures never
  reach here.
- this node is a single-shot LLM judge for what rules cannot check:
  business semantics ("does this result mean what the question asks").

The judge deliberately gets NO tools: an LLM judge with a SQL executor
turns into an explorer and never converges (measured: DeepSeek hit the
round guard on nearly every question). Judgment is one call — the
verdict is still the model's, and the correction loop downstream
(analyze_error → regenerate) handles rejections.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.i18n import L
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.workflow.intent import has_weak_signal
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

REFLECT_SYSTEM_PROMPT_ZH = """你是严格的 SQL 结果评估器。检查查询结果是否正确回答了用户的问题。

注意：结构类检查（行数形态、数值类型、百分比范围等）已由确定性规则通过，你只负责业务语义把关。

严格一点：静默的错误结果比可见的错误更糟。不确定时返回 RETRY 并给出具体理由。

评估要点：
1. 结果是否符合问题的逻辑？
2. 列名是否符合问题所问？
3. 数值的量级、单位、业务逻辑是否合理（如年龄小于 120、百分比在 0-100）？
4. SQL 是否真正实现了问题所问的语义（而非表面相似）？
5. 若提供了官方证据（Evidence），SQL 与结果是否符合证据？
6. 列是否冗余或缺失？结果列应精确匹配问题所问，不多不少。
7. 列顺序是否符合问题要求？问题强调或先问到的列应排在前面。
8. 条件完整性（双向映射）：问题要求的每个条件都出现在 SQL 中了吗？SQL 中每个 WHERE 条件都能在问题里找到依据吗？条件缺失（尤其"在…中"限定的属性条件，如周发放）或多余都是错误，直接判 RETRY。

判定护栏：
- 判 "EMPTY"（空结果但 SQL 正确）之前，先排除 SQL 过滤条件过严的可能（如时间范围、WHERE 条件把应有结果滤掉了）；只要可疑，判 "RETRY" 并说明理由。
- 空结果判 "OK" 需格外谨慎。
- 对目标方言的特定函数或语法行为不确定时，判 "RETRY" 并说明理由。
- 不要重新争论问题本身的歧义：只要 SQL 与问题的某一种合理解读一致（条件集与问题要求一一对应、极值在该解读限定的集合上取），就判 OK；不要以"另一种解读也可能对"为由 RETRY。
- 问题要求最低/最高时，ORDER BY + LIMIT 1 或 MIN/MAX 子查询是标准写法，直接接受；不要以"并列值可能漏行"为由 RETRY（除非问题明确要求返回全部并列者）。
- Evidence 给出的公式或定义（如 "Gap = X - Y"）就是权威语义：按其字面执行（作用域是全局，除非 Evidence 明确限定范围），SQL 与公式一致即通过；不得自行附加人群/性别/年龄等范围限定，也不得以"公式作用域可能有别的理解"为由 RETRY。

只回答以下之一：
- "OK" — 结果满意
- "RETRY: <理由>" — 结果错误，需要重新生成（说明哪里错了）
- "EMPTY" — 结果为空但 SQL 正确（数据确实无匹配）
- "NO_SQL: <理由>" — 问题本身不是数据查询（表含义、术语定义、知识性问题），任何 SQL 结果都无法回答它
"""

REFLECT_SYSTEM_PROMPT = """You are a SQL result evaluator. Your task is to check whether the query results correctly answer the user's question.

Note: structural checks (row shape, dtype, value range) have already passed deterministic rules — you judge business semantics only.

Be strict: a silent wrong result is worse than a visible one. When in doubt, return RETRY with a specific reason.

Evaluate on:
1. Does the result make logical sense for the question?
2. Do the column names match what the question asks for?
3. Are the values in a reasonable range and unit?
4. Does the SQL actually implement the semantics the question asks (not just a surface resemblance)?
5. If official evidence is provided, do the SQL and result comply with it?
6. Are columns redundant or missing? The result columns should exactly match what the question asks — no more, no fewer.
7. Does the column order match the question's expectation? Columns the question emphasizes or asks for first should come first.
8. Condition completeness (two-way mapping): does every condition the question requires appear in the SQL? Can every WHERE condition in the SQL be justified by the question? A missing condition (especially an attribute condition inside an "among" qualifier, e.g. weekly issuance) or an extra one is an error — judge RETRY.

Decision guardrails:
- Before judging "EMPTY" (empty result but correct SQL), rule out over-restrictive filters (e.g. a time range or WHERE clause that filtered out rows that should be there); when in doubt, judge "RETRY" and explain.
- Be extra cautious before judging an empty result as "OK".
- If unsure about a dialect-specific function or syntax behavior, judge "RETRY" and explain.
- Do not re-argue the question's own ambiguity: if the SQL is consistent with any reasonable interpretation of the question (condition set matches what the question requires, extreme taken over that interpretation's set), judge OK — never RETRY merely because another interpretation could also be possible.
- When the question asks for the lowest/highest, ORDER BY + LIMIT 1 or a MIN/MAX subquery is the standard form — accept it; do not RETRY over possible ties (unless the question explicitly requires returning all tied rows).
- A formula or definition given in the Evidence (e.g. "Gap = X - Y") is the authoritative semantics: apply it literally (global scope unless the Evidence explicitly restricts it). If the SQL matches the formula, pass — do not invent population/gender/age restrictions, and do not RETRY on the grounds that the formula's scope could be understood differently.

Respond with ONE of:
- "OK" — the result is satisfactory
- "RETRY: <reason>" — the result is wrong and needs regenerating
- "EMPTY" — the result is empty but the SQL looks correct (data might not exist)
- "NO_SQL: <reason>" — the question is not a data query (table meaning, term definition, knowledge question); no SQL result can answer it
"""

MAX_TOTAL_RETRIES = 10
MAX_SEMANTIC_RETRIES = 3  # 连续纯语义 RETRY 上限(执行成功后仍被连续打回)


def make_reflect(
    llm: LLMGateway,
    config: AgentConfig,
    max_retries: int = MAX_TOTAL_RETRIES,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the reflect node bound to an LLM gateway.

    Single-shot judge: one LLM call, no tools. RETRY verdicts are only
    issued while retry_count < max_retries (shared correction budget).
    """

    async def reflect(state: WorkflowState) -> dict[str, Any]:
        # Upstream node failed — pass through without running
        if state.error:
            return {}

        # Fast path: empty result is acceptable (no data matches).
        # List questions returning no rows are already intercepted by
        # deterministic rules before this node. Questions with a metadata
        # leaning still go to the LLM judge — an empty result for a
        # definitional question should be able to verdict NO_SQL.
        if state.row_count == 0 and not has_weak_signal(state.question):
            return {
                "verdict": "EMPTY",
                "reason": "Query returned zero rows — this may be correct if no data matches",
            }

        prompt = _build_reflect_prompt(
            state.question, state.columns, state.rows[:10], state.row_count,
            schema_context=state.schema_context,
            sql=state.sql,
            evidence=state.evidence,
            time_context=state.time_context,
        )

        try:
            model = config.target or "openai/gpt-4o"
            system_prompt = L(
                state.lang,
                REFLECT_SYSTEM_PROMPT_ZH,
                REFLECT_SYSTEM_PROMPT,
            )
            start = time.monotonic()
            response = await llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=300,
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
                return {"verdict": "OK", "semantic_retries": 0, "llm": llm_detail}
            elif verdict.startswith("RETRY"):
                reason = response.replace("RETRY:", "").replace("RETRY", "").strip()
                # 纯语义 RETRY(上一次执行成功、无执行错误):欠定问题法官
                # 可能无限重审,连续打回超过上限即强制接受——预算花在
                # 执行错误/规则违反上,不花在语义拉锯上。
                semantic = not state.error_feedback
                semantic_retries = state.semantic_retries + 1 if semantic else 0
                if (
                    state.retry_count >= max_retries
                    or semantic_retries >= MAX_SEMANTIC_RETRIES
                ):
                    logger.warning(
                        "Retry cap reached (total=%d, semantic=%d); accepting result despite issues",
                        state.retry_count, semantic_retries,
                    )
                    return {
                        "verdict": "OK", "forced": True, "reason": reason,
                        "semantic_retries": 0, "llm": llm_detail,
                    }

                return {
                    "verdict": "RETRY",
                    "reason": reason,
                    "retry_count": state.retry_count + 1,
                    "semantic_retries": semantic_retries,
                    "llm": llm_detail,
                }
            elif verdict.startswith("NO_SQL"):
                # The question is not answerable by SQL — route to the
                # metadata answer path instead of regenerating. Not a
                # retry: no retry_count increment, no forced-OK at cap.
                reason = re.sub(r"(?i)no_sql\s*:?\s*", "", response).strip()
                return {
                    "verdict": "NO_SQL",
                    "reason": reason,
                    "no_sql": True,
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
    schema_context: str = "",
    sql: str = "",
    evidence: str = "",
    time_context: str = "",
) -> str:
    """Build the reflection evaluation prompt."""
    sample = ""
    if sample_rows:
        sample = "\n".join(
            str(row) for row in sample_rows[:5]
        )

    schema_block = ""
    if schema_context:
        schema_block = f"Schema context:\n{schema_context[:1200]}\n\n"

    sql_block = ""
    if sql:
        sql_block = f"Generated SQL:\n{sql}\n\n"

    evidence_block = ""
    if evidence:
        evidence_block = f"Evidence (official hint, authoritative):\n{evidence}\n\n"

    time_block = ""
    if time_context:
        time_block = f"Resolved time range (authoritative):\n{time_context}\n\n"

    return (
        f"User question: {question}\n\n"
        f"{evidence_block}"
        f"{time_block}"
        f"{schema_block}"
        f"{sql_block}"
        f"Result columns: {columns}\n"
        f"Total rows returned: {total_rows}\n"
        f"Sample rows (first 5):\n{sample}\n\n"
        f"Does this result correctly answer the user's question?\n"
        f"Respond with OK, RETRY: <reason>, EMPTY, or NO_SQL: <reason>."
    )
