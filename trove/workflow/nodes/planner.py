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


def validate_plan(
    plan: dict[str, Any] | None, schema: dict[str, set[str]] | None,
) -> list[str]:
    """校验计划引用的表/列真实存在(层1,确定性,零 LLM)。

    schema: 小写表名 → 小写列名集合(来自 connectors.get_schema())。
    表达式(含括号)、通配符 *、空字段跳过——只有直接列引用需要核实。
    返回错误列表(空 = 合法)。plan 或 schema 不可用 → 无法校验,返回空。
    """
    if not plan or not schema:
        return []
    errors: list[str] = []
    table_map = schema
    tables = [str(t) for t in (plan.get("tables") or [])]
    for t in tables:
        if t.lower() not in table_map:
            errors.append(f"table '{t}' not in schema")

    def check_field(field: Any, where: str) -> None:
        f = str(field or "").strip()
        if not f or f == "*" or "(" in f:
            return
        if "." in f:
            tbl, col = f.split(".", 1)
            if tbl.lower() not in table_map:
                errors.append(f"{where}: table '{tbl}' not in schema")
            elif col.lower() not in table_map[tbl.lower()]:
                errors.append(f"{where}: column '{col}' not in table '{tbl}'")
            return
        if not tables:
            errors.append(f"{where}: column '{f}' referenced but plan lists no tables")
        elif not any(
            f.lower() in table_map[t.lower()]
            for t in tables if t.lower() in table_map
        ):
            errors.append(f"{where}: column '{f}' not found in planned tables")

    for ac in plan.get("answer_columns") or []:
        check_field(ac, "answer_columns")
    for c in plan.get("conditions") or []:
        if isinstance(c, dict):
            check_field(c.get("field"), "conditions")
    return errors


def answer_columns_mismatch(
    plan_json: dict[str, Any] | None, result_columns: list[str],
) -> list[str]:
    """plan 的 answer_columns 与执行结果列的一致性检查(层2,确定性)。

    仅当 answer_columns 里所有直接列引用都不在结果列中出现时才判定
    冲突——任一命中即放行(别名/表达式会让单列不一致成为常态噪音,
    全部缺失才是 SELECT 列表整体背离计划的强信号)。
    返回冲突描述列表(空 = 通过)。
    """
    if not plan_json:
        return []
    refs = [
        str(ac).strip() for ac in (plan_json.get("answer_columns") or [])
        if str(ac or "").strip() and str(ac).strip() not in ("*", "") and "(" not in str(ac)
    ]
    if not refs:
        return []
    lower_result = {str(c).lower() for c in result_columns}
    missing = [
        r for r in refs
        if r.lower() not in lower_result
        and r.split(".", 1)[-1].lower() not in lower_result
    ]
    if len(missing) < len(refs):
        return []
    return [
        f"answer_columns {refs} conflict with result columns {list(result_columns)}"
    ]


async def _schema_map(connectors) -> dict[str, set[str]] | None:
    """真实 schema → 小写表名 → 小写列名集合;不可用 → None(跳过校验)。"""
    if connectors is None:
        return None
    try:
        schema = await connectors.get_schema()
        return {
            t.name.lower(): {c.name.lower() for c in t.columns}
            for t in schema.tables
        }
    except Exception:
        return None


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
        base_correction = " ".join(
            p for p in (state.error_feedback, state.error_analysis, state.reason) if p
        )
        schema_map = await _schema_map(connectors)
        model = config.target or "openai/gpt-4o"
        system_prompt = render("planner/system", lang=state.lang)
        llm_detail: dict[str, Any] | None = None
        trail = ""

        async def call_planner(correction: str) -> str:
            nonlocal llm_detail, trail
            prompt = render(
                "planner/user",
                lang=state.lang,
                question=state.question,
                schema_context=state.schema_context[:1500],
                evidence=state.evidence,
                time_context=state.time_context,
                history=state.history,
                correction=correction[:600] if correction else "",
                previous_plan=state.plan[:800] if state.plan else "",
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
                t = " ".join(
                    p for p in (result.get("reasoning", ""), result.get("transcript", "")) if p
                )
                trail = t[:800]
                return result["content"]

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
            llm_detail = {
                "model": model,
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "input_preview": prompt[:200],
                "output_preview": (response or "").strip()[:200],
            }
            return response

        try:
            # 层1(plan 落地校验):引用的表/列必须真实存在;失败带修正
            # 自修正一次,仍失败则丢弃 plan(gen_sql 无 plan 照常生成,
            # 校验只拦截幻觉列,不让它变成 gen_sql 的钦点指令)
            raw = await call_planner(base_correction)
            plan_json = _parse_plan(raw)
            plan = _plan_text(raw, state.lang)
            errors = validate_plan(plan_json, schema_map)
            if errors:
                fix_correction = (
                    base_correction
                    + f" Your previous plan was invalid: {'; '.join(errors)}. "
                    + "Fix the plan so every table and column reference exists in the schema."
                )
                raw = await call_planner(fix_correction)
                plan_json = _parse_plan(raw)
                plan = _plan_text(raw, state.lang)
                errors = validate_plan(plan_json, schema_map)
            if errors:
                logger.info("Plan dropped after validation: %s", "; ".join(errors))
                update: dict[str, Any] = {
                    "plan": "",
                    "plan_json": None,
                    "plan_validation": {"status": "dropped", "errors": errors},
                }
                if llm_detail:
                    update["llm"] = llm_detail
                return update
            if not plan:
                return {}
            update = {
                "plan": plan,
                "plan_json": plan_json,
                "plan_validation": {"status": "ok"},
            }
            if llm_detail:
                update["llm"] = llm_detail
            if trail:
                update["reasoning_history"] = [{"node": "planner", "text": trail}]
            return update
        except Exception as e:
            logger.warning("Planner failed (proceeding without a plan): %s", e)
            return {}

    return planner
