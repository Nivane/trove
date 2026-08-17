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
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.llm.agent_loop import run_agent_loop
from trove.prompts import render
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

        # 回退重跑：携带上一次失败与诊断，重定计划而不是重写原计划
        correction = " ".join(
            p for p in (state.error_feedback, state.error_analysis, state.reason) if p
        )
        prompt = render(
            "planner/user",
            lang=state.lang,
            question=state.question,
            schema_context=state.schema_context[:1500],
            time_context=state.time_context,
            history=state.history,
            correction=correction[:600] if correction else "",
            previous_plan=state.plan[:800] if state.plan else "",
        )

        try:
            model = config.target or "openai/gpt-4o"
            system_prompt = render("planner/system", lang=state.lang)
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
