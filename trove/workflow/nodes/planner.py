"""Planner node — LLM drafts a concise query plan before SQL generation.

The plan (tables, joins, aggregations, filters, ordering) is injected
into the gen_sql prompt as a "Query plan" section — the two-step
plan-then-write flow. Planner failures are silent (empty plan): the
pipeline never blocks on planning.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.i18n import L
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.llm.agent_loop import run_agent_loop
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)


def _parse_plan(response: str) -> dict[str, Any] | None:
    """结构化计划解析:摘掉可能的 markdown 围栏后按 JSON 解析。

    返回 None 表示模型没按格式输出(散文计划)——调用方原样回退,管线不中断。
    """
    text = (response or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _render_plan(data: dict[str, Any], lang: str = "en") -> str:
    """结构化计划 → 注入 gen_sql 提示词的文本(条件逐行,作用域显式)。"""
    zh = lang == "zh"
    lines: list[str] = []
    if data.get("tables"):
        lines.append(("表: " if zh else "Tables: ") + ", ".join(map(str, data["tables"])))
    if data.get("joins"):
        lines.append(("关联: " if zh else "Joins: ") + str(data["joins"]))
    conditions = data.get("conditions") or []
    if conditions:
        lines.append("条件:" if zh else "Conditions:")
        for c in conditions:
            note = f"（{c['note']}）" if zh and c.get("note") else f" ({c['note']})" if c.get("note") else ""
            lines.append(f"  - {c.get('field')} {c.get('op')} {c.get('value')}{note}")
    if data.get("aggregation"):
        lines.append(("聚合: " if zh else "Aggregation: ") + str(data["aggregation"]))
    extreme = data.get("extreme")
    if extreme:
        scope = extreme.get("scope", "")
        lines.append(
            f"{('极值: ' if zh else 'Extreme: ')}{extreme.get('func')}({extreme.get('column')})"
            f" · scope: {scope}"
        )
    if data.get("ordering"):
        lines.append(("排序: " if zh else "Ordering: ") + str(data["ordering"]))
    if data.get("answer_columns"):
        lines.append(
            ("输出列: " if zh else "Answer columns: ") + ", ".join(map(str, data["answer_columns"]))
        )
    return "\n".join(lines)


def _plan_text(response: str, lang: str) -> str:
    """LLM 回复 → 计划文本:JSON 结构化渲染,解析失败回退散文原文。"""
    data = _parse_plan(response)
    return _render_plan(data, lang) if data is not None else (response or "").strip()

PLANNER_SYSTEM_PROMPT_ZH = """你是 SQL 查询规划器。根据用户问题和相关表结构，起草一份查询计划，直接输出 JSON 对象（不要 markdown 围栏、不要输出其它文字）：

{"tables": ["loan", "account"], "joins": "loan.account_id = account.account_id", "conditions": [{"field": "loan.date", "op": "=", "value": "1997", "note": "贷款批准年份"}], "aggregation": "无 | count | sum | avg | ...", "extreme": {"func": "min|max", "column": "loan.amount", "scope": "全部条件过滤后 | 全局"}, "ordering": "字段 升/降序（无则留空）", "answer_columns": ["account_id"]}

关键作用域规则：当问题用「在…中 / among / whose」等限定多个条件（含属性条件，如"选择周发放"），并要求最低/最高/最多/最少时，必须先把全部限定条件应用于过滤，再在过滤后的集合上取极值（或排序取第一条）。极值的作用域永远是被全部限定条件筛选后的集合——绝不能在应用全部限定条件之前先算极值。extreme.scope 必须显式写明极值在哪个集合上取。

输出列规则：answer_columns 只放问题明确要求的列。
- "list all the X ..." 类问题若未点名列，answer_columns 只含 X 的标识列（其 ID，如 trans_id、account_id）或问题点名的属性列；不要罗列记录的明细列（date、amount、balance、type、operation、status 等）。
- 公式类问题（rate/gap/percentage/increment）answer_columns 只含公式的最终结果列——不含公式的输入列（如 A12/A13），也不含实体名列，除非问题用清晰通顺的并列结构同时点名实体及其指标（"list the districts and their unemployment rate"）。问题句式残缺、语法破碎时（如 "the district of the and the state"），只输出公式的最终结果列。
- 不要把仅用于排序的列放进来。"""

PLANNER_SYSTEM_PROMPT = """You are a SQL query planner. Given the user question and the relevant schema, draft a query plan as a JSON object only (no markdown fences, no extra text):

{"tables": ["loan", "account"], "joins": "loan.account_id = account.account_id", "conditions": [{"field": "loan.date", "op": "=", "value": "1997", "note": "loan approval year"}], "aggregation": "none | count | sum | avg | ...", "extreme": {"func": "min|max", "column": "loan.amount", "scope": "after all filters | global"}, "ordering": "column asc/desc (empty if none)", "answer_columns": ["account_id"]}

Scoping rule: when the question qualifies a set with multiple conditions (among/whose, including attribute conditions like "choose weekly issuance") and asks for the lowest/highest/most/fewest, apply ALL qualifying conditions as filters FIRST, then take the extreme (or order and take the first row) within that filtered set. The scope of the extreme is always the set after every qualifier has been applied — never compute the extreme before applying all qualifiers. The extreme.scope field must state explicitly which set the extreme is taken over.

Answer columns rule: put in answer_columns ONLY the columns the question asks for.
- For a "list all the X ..." question that names no columns, answer_columns contains only the identifying column of X (its ID — e.g. trans_id, account_id) or the attribute the question names; never enumerate the record's detail columns (date, amount, balance, type, operation, status, ...).
- For formula questions (rate/gap/percentage/increment), answer_columns contains only the final formula column — not the formula's input columns (e.g. A12/A13) and not entity-name columns, unless the question uses a clear grammatical parallel structure naming both entities and their measure ("list the districts and their unemployment rate"). When the question's phrasing is broken or garbled (e.g. "the district of the and the state"), output only the final formula column.
- Never list columns used only for ordering."""


def make_planner(
    llm: LLMGateway,
    config: AgentConfig,
    agentic: bool = True,
    connectors=None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the planner node bound to an LLM gateway."""

    async def planner(state: WorkflowState) -> dict[str, Any]:
        # Upstream failure — pass through
        if state.error:
            return {}

        prompt_parts = [
            f"Question: {state.question}",
            f"Schema context:\n{state.schema_context[:1500]}",
        ]
        if state.time_context:
            prompt_parts.append(f"Resolved time range: {state.time_context}")
        if state.history:
            prompt_parts.append(f"Conversation history:\n{state.history}")
        # 回退重跑：携带上一次失败与诊断，重定计划而不是重写原计划
        correction = " ".join(
            p for p in (state.error_feedback, state.error_analysis, state.reason) if p
        )
        if correction:
            prompt_parts.append(L(
                state.lang,
                f"修正上下文（上一次失败）:\n{correction[:600]}",
                f"Correction context (previous failure):\n{correction[:600]}",
            ))
            # 增量修订:上一版计划此刻仍在 state.plan 里,带给模型对照
            if state.plan:
                prompt_parts.append(L(
                    state.lang,
                    f"上一版计划（已被判失败，保留其中仍正确的部分）:\n{state.plan[:800]}",
                    f"Previous plan (judged failed; keep the parts that remain correct):\n{state.plan[:800]}",
                ))
        prompt = "\n\n".join(prompt_parts)

        try:
            model = config.target or "openai/gpt-4o"
            system_prompt = L(
                state.lang,
                PLANNER_SYSTEM_PROMPT_ZH,
                PLANNER_SYSTEM_PROMPT,
            )
            if agentic and connectors is not None:
                async def table_columns(arguments: dict) -> str:
                    table = arguments.get("table", "")
                    schema = await connectors.get_schema()
                    for t in schema.tables:
                        if t.name.lower() == table.lower():
                            return ", ".join(f"{c.name} {c.type}" for c in t.columns)
                    return f"table '{table}' not found"

                result = await run_agent_loop(
                    llm, model,
                    system=system_prompt,
                    user=prompt,
                    tools=[{
                        "type": "function",
                        "function": {
                            "name": "get_table_columns",
                            "description": "Inspect the columns of one table.",
                            "parameters": {
                                "type": "object",
                                "properties": {"table": {"type": "string"}},
                                "required": ["table"],
                            },
                        },
                    }],
                    tool_handlers={"get_table_columns": table_columns},
                    max_rounds=5,
                    metadata={"node": "planner", "session_id": state.session_id, "run_id": state.run_id},
                )
                plan = _plan_text(result["content"], state.lang)
                update = {"plan": plan} if plan else {}
                trail = " ".join(
                    p for p in (result.get("reasoning", ""), result.get("transcript", "")) if p
                )
                if trail:
                    update["reasoning_history"] = [{"node": "planner", "text": trail[:800]}]
                return update

            start = time.monotonic()
            response = await llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                metadata={
                    "node": "planner",
                    "session_id": state.session_id,
                    "run_id": state.run_id,
                    "question": state.question[:80],
                },
            )
            plan = _plan_text(response, state.lang)
            llm_detail = {
                "model": model,
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "input_preview": prompt[:200],
                "output_preview": plan[:200],
            }
            return {"plan": plan, "llm": llm_detail} if plan else {}
        except Exception as e:
            logger.warning("Planner failed (proceeding without a plan): %s", e)
            return {}

    return planner
