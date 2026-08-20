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
(analyze_error → regenerate) handles rejections. Exception: a pure
semantic RETRY (execution succeeded, rules passed) is re-judged once
with an independent sample — one judge call is a coin flip (measured:
the same correct result was rejected twice with contradictory reasons),
and only two matching RETRYs send the result back to regeneration.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.prompts import render
from trove.workflow.intent import has_weak_signal
from trove.workflow.state import WorkflowState, budget_exhausted

logger = get_logger(__name__)

MAX_TOTAL_RETRIES = 10
# 连续纯语义 RETRY 上限(执行成功后仍被连续打回)。cap=2:第一次一致性
# RETRY 换一轮重生成,第二次即强制接受——语义拉锯最多烧 1 轮预算。
MAX_SEMANTIC_RETRIES = 2
# rejudge 采样温度:主裁决 temp=0(确定性),第二次裁决拉高温度取独立样本,
# 才构成真正的"第二次意见"(同温度同 prompt 会复读同一裁决)。
REJUDGE_TEMPERATURE = 0.7


def _projection_width_matches(sql: str, dialect: str, actual_columns: int) -> bool:
    """自洽检查:顶层 SELECT 投影列数 == 执行结果列数。

    ``state.sql`` 与 ``state.columns`` 由 execute_sql / select 原子写入,
    宽度一致是"结果确实来自该 SQL"的确定性旁证。SELECT * / 解析失败 /
    非查询 → False(不跳过,交给 LLM 法官,保守方向)。
    """
    if actual_columns <= 0:
        return False
    try:
        import sqlglot
        from sqlglot import exp

        parsed = sqlglot.parse_one(sql, dialect=dialect, error_level=sqlglot.ErrorLevel.RAISE)
    except Exception:
        return False
    node = parsed
    if isinstance(node, exp.With):  # CTE:取外层查询,不是 WITH 体
        node = node.this
    if not isinstance(node, exp.Select):
        node = node.find(exp.Select)
        if node is None:
            return False
    exprs = node.expressions
    if not exprs or any(isinstance(e, exp.Star) for e in exprs):
        return False  # SELECT * → 宽度不可验证
    return len(exprs) == actual_columns


def _extract_verdict(response: str) -> str:
    """从回复中提取裁决词:逐行从末尾向前找 OK/EMPTY/RETRY:/NO_SQL: 行。

    推理模型(DeepSeek)常把整段 reasoning 放在 content 里、裁决词单列
    在末尾行。行首精确匹配避免把正文中提到的 "OK" 误当裁决。
    找不到时返回原文(调用方按不可解析处理)。
    """
    for line in reversed((response or "").splitlines()):
        m = re.match(r"^\s*(OK|EMPTY)\s*$", line, re.I)
        if m:
            return m.group(1).upper()
        m = re.match(r"^\s*(RETRY|NO_SQL)\s*:?\s*(.*)$", line, re.I)
        if m:
            suffix = m.group(2).strip()
            return f"{m.group(1).upper()}: {suffix}" if suffix else m.group(1).upper()
    return (response or "").strip()


async def _reask_verdict(llm: LLMGateway, model: str, state: WorkflowState) -> str:
    """极简二次裁决:主裁决不可解析时,用最短 prompt 再问一次。

    短 prompt 让推理模型的 reasoning 在预算内收敛,content 才能带出
    裁决词。仍不可解析时返回空串(调用方强制放行)。
    """
    sample = "\n".join(str(row) for row in state.rows[:3])
    prompt = render(
        "reflect/reask_user",
        question=state.question,
        columns=state.columns,
        sample=sample,
    )
    try:
        response = await llm.chat(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": render("reflect/reask_system"),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=16000,
            metadata={
                "node": "reflect_reask",
                "session_id": state.session_id,
                "run_id": state.run_id,
                "question": state.question[:80],
            },
        )
        return _extract_verdict(response)
    except Exception as e:
        logger.warning("Reflect re-ask failed: %s", e)
        return ""


async def _rejudge_verdict(
    llm: LLMGateway,
    model: str,
    state: WorkflowState,
    system_prompt: str,
    prompt: str,
) -> tuple[str, dict[str, Any]]:
    """第二次独立语义裁决:同证据、更高温度采样。

    主裁决判纯语义 RETRY 时调用——一次 LLM 判断是"掷硬币"(实测:同一
    正确答案被两次相反理由打回)。只有两次裁决一致判 RETRY 才回退重生成;
    rejudge 失败/不可解析视为"无第二次意见",不构成一致(调用方放行)。
    返回 (verdict, detail);失败返回 ("", detail)。
    """
    start = time.monotonic()
    detail: dict[str, Any] = {"elapsed_ms": 0}
    try:
        response = await llm.chat(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=REJUDGE_TEMPERATURE,
            max_tokens=16000,
            metadata={
                "node": "reflect_rejudge",
                "session_id": state.session_id,
                "run_id": state.run_id,
                "question": state.question[:80],
            },
        )
        detail["elapsed_ms"] = int((time.monotonic() - start) * 1000)
        detail["output_preview"] = response[:200]
        return _extract_verdict(response), detail
    except Exception as e:
        logger.warning("Reflect rejudge failed: %s", e)
        detail["elapsed_ms"] = int((time.monotonic() - start) * 1000)
        return "", detail


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

        # KB 精确命中:SQL 直接取自 KB 标准写法(未经过模型生成),
        # 执行与确定性规则都已通过——跳过语义裁决,避免法官对
        # 金标准答案做无谓的语义拉锯(实测:gold SQL 被连打回 2 轮)。
        if state.kb_exact_match and state.sql:
            return {
                "verdict": "OK",
                "reason": "KB exact match (canonical answer from the knowledge base)",
            }

        # 确定性快径命中:模板 SQL 是 kb init 的确定性产物(结构受
        # sqlglot 形状约束,执行与规则链已过)——与 kb_exact_match
        # 同理由,跳过语义裁决。
        if state.fast_path and state.sql:
            return {
                "verdict": "OK",
                "reason": "fast path deterministic template match (kb init)",
            }

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

        # 规则全过 + 低复杂度 → 跳过 LLM 裁决(reflect_skip 配置控制)。
        # 确定性规则链(形状/过滤/值域)对简单查询已是完整的安全网,语义
        # 裁决主要是给复杂 SQL 的盲区兜底。弱信号问题保留法官(metadata
        # 倾向题需要 NO_SQL 出口,镜像上方 EMPTY 分支)。
        # reflect_skip 是档位阶梯: simple(默认,只跳 simple) < standard
        # (再跳 standard) < all(全跳)。off=全不跳。
        # 追加的确定性证据:row_count > 0(0 行由上方 EMPTY 分支处理,
        # 弱信号问题需保留法官)+ 投影宽度自洽(执行结果列数 == SQL
        # SELECT 列数)——两者不满足即退回 LLM 裁决,保守方向。
        _skip_levels = {"simple": 1, "standard": 2, "all": 3}
        _complexity_levels = {"simple": 1, "standard": 2, "complex": 3}
        skip = config.reflect_skip or "simple"
        if (
            skip != "off"
            and state.rules_passed
            and not state.error_feedback
            and not has_weak_signal(state.question)
            and state.row_count > 0
            and _skip_levels.get(skip, 1)
            >= _complexity_levels.get(state.complexity, 2)
            and _projection_width_matches(
                state.sql, state.dialect, len(state.columns),
            )
        ):
            return {
                "verdict": "OK",
                "reason": "deterministic rules passed; reflect skipped",
            }

        prompt = _build_reflect_prompt(
            state.question, state.columns, state.rows[:10], state.row_count,
            schema_context=state.schema_context,
            sql=state.sql,
            evidence=state.evidence,
            time_context=state.time_context,
        )

        try:
            model = config.model_for(state.complexity)
            system_prompt = render("reflect/system", lang=state.lang)
            start = time.monotonic()
            response = await llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                # 推理模型会把预算花在 reasoning 上(实测小预算导致 content 为空、
                # 裁决从未产出);统一放宽输出上限(见 gateway 默认值)
                max_tokens=16000,
                metadata={
                    "node": "reflect",
                    "session_id": state.session_id,
                    "run_id": state.run_id,
                    "question": state.question[:80],
                },
            )
            verdict = _extract_verdict(response)
            llm_detail = {
                "model": model,
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "input_preview": prompt[:200],
                "output_preview": response[:200],
            }

            if verdict == "" or verdict == "NONE" or not re.match(r"^(OK|RETRY|NO_SQL|EMPTY)\b", verdict):
                # 空输出/不可解析 = 语义检查没有发生(推理模型常把预算花在
                # reasoning 上,content 为空)。用极简 prompt 再问一次,给
                # 法官一次真正的裁决机会;仍不可解析就强制放行——语义安全
                # 由确定性规则兜底,而不是让一个失效的法官反复把正确结果
                # 推入升温重生成(实测:正确答案被无谓 RETRY 改坏)。
                verdict = await _reask_verdict(llm, model, state)
            if verdict == "" or verdict == "NONE" or not re.match(r"^(OK|RETRY|NO_SQL|EMPTY)\b", verdict):
                reason = (
                    "the judge could not verify the result (empty or unparseable "
                    "verdict) — result delivered as-is; deterministic rules already "
                    "checked the structural properties"
                )
                logger.warning("Reflect judge unparseable after re-ask; delivering result")
                return {
                    "verdict": "OK", "forced": True, "reason": reason,
                    "semantic_retries": 0, "llm": llm_detail,
                }

            if verdict.startswith("OK"):
                return {"verdict": "OK", "semantic_retries": 0, "llm": llm_detail}
            elif verdict.startswith("RETRY"):
                reason = re.sub(r"(?i)^retry\s*:?\s*", "", verdict).strip()
                # 纯语义 RETRY(上一次执行成功、无执行错误):法官在质疑一个
                # 确定性规则已放行的结果。一次 LLM 判断不足以烧掉一轮
                # "打回→重生成"预算——先取第二次独立裁决,两次一致判
                # RETRY 才回退;不一致(或 rejudge 失败)即视为裁决不可靠,
                # 结果交付(实测:法官把 few-shot 范围文本误读为全局断言,
                # 同一正确答案被两次相反理由打回)。
                semantic = not state.error_feedback
                if budget_exhausted(state.retry_count, max_retries):
                    logger.warning(
                        "Retry cap reached (total=%d); accepting result despite issues",
                        state.retry_count,
                    )
                    return {
                        "verdict": "OK", "forced": True, "reason": reason,
                        "semantic_retries": 0, "llm": llm_detail,
                    }
                rejudge_detail: dict[str, Any] = {}
                if semantic:
                    rejudge_verdict, rejudge_detail = await _rejudge_verdict(
                        llm, model, state, system_prompt, prompt,
                    )
                    if not rejudge_verdict.startswith("RETRY"):
                        logger.warning(
                            "Reflect judge disagreement (first=%r, second=%r); delivering result",
                            verdict, rejudge_verdict or "<unparseable/failed>",
                        )
                        return {
                            "verdict": "OK", "forced": True,
                            "reason": (
                                "judge disagreement (first RETRY, second "
                                f"{rejudge_verdict or 'unavailable'}) — result "
                                "delivered; deterministic rules already checked "
                                "the structural properties"
                            ),
                            "semantic_retries": 0,
                            "llm": {**llm_detail, "rejudge": rejudge_detail},
                        }
                # 两次一致判 RETRY(或非语义 RETRY)→ 回退 analyze_error。
                # 计数单调累计、不被执行错误重置(否则"打回→改坏→再打回"
                # 可无限交替),达上限即强制接受。
                semantic_retries = (
                    state.semantic_retries + 1 if semantic else state.semantic_retries
                )
                if semantic_retries >= MAX_SEMANTIC_RETRIES:
                    logger.warning(
                        "Retry cap reached (total=%d, semantic=%d); accepting result despite issues",
                        state.retry_count, semantic_retries,
                    )
                    return {
                        "verdict": "OK", "forced": True, "reason": reason,
                        "semantic_retries": 0,
                        "llm": {**llm_detail, **({"rejudge": rejudge_detail} if rejudge_detail else {})},
                    }

                return {
                    "verdict": "RETRY",
                    "reason": reason,
                    "retry_count": state.retry_count + 1,
                    "semantic_retries": semantic_retries,
                    "llm": {**llm_detail, **({"rejudge": rejudge_detail} if rejudge_detail else {})},
                }
            elif verdict.startswith("NO_SQL"):
                # The question is not answerable by SQL — route to the
                # metadata answer path instead of regenerating. Not a
                # retry: no retry_count increment, no forced-OK at cap.
                reason = re.sub(r"(?i)no_sql\s*:?\s*", "", verdict).strip()
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
    """Build the reflection evaluation prompt.

    Thin wrapper over the ``reflect/user`` Jinja template.
    """
    sample = ""
    if sample_rows:
        sample = "\n".join(
            str(row) for row in sample_rows[:5]
        )
    # schema 上限 10000 字符:裁决需要足够 schema 才能判断连接/过滤是否
    # 成立(实测 600 连联表都看不清)。推理模型把预算花在 reasoning 上导致
    # content 为空的失败已由 _reask_verdict 极简重问 + 强制放行兜底。
    return render(
        "reflect/user",
        question=question,
        evidence=evidence,
        time_context=time_context,
        schema_context=schema_context[:10000] if schema_context else "",
        sql=sql,
        columns=columns,
        total_rows=total_rows,
        sample=sample,
    )
