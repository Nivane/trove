"""Planner node — LLM drafts a concise query plan before SQL generation.

The plan (tables, joins, aggregations, filters, ordering) is injected
into the gen_sql prompt as a "Query plan" section — the two-step
plan-then-write flow. Planner failures are silent (empty plan): the
pipeline never blocks on planning.
"""

from __future__ import annotations

import asyncio
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
from trove.prompts.skills import render_skills
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
            # 模型偶发把条件输出成字符串数组(而非对象数组)——原样展示
            # 而不是崩溃丢整个 plan(崩溃 → 无计划 → SQL 质量下降 → RETRY 级联)
            if not isinstance(c, dict):
                lines.append(f"  - {c}")
                continue
            note = f"（{c['note']}）" if zh and c.get("note") else f" ({c['note']})" if c.get("note") else ""
            lines.append(f"  - {c.get('field')} {c.get('op')} {c.get('value')}{note}")
    if data.get("aggregation"):
        lines.append(("聚合: " if zh else "Aggregation: ") + str(data["aggregation"]))
    extreme = data.get("extreme")
    if isinstance(extreme, dict):
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


def _word_in_question(column: str, question_lower: str) -> bool:
    """列名(去表限定尾缀)是否以单词形式出现在问题文本中(单复数、下划线变体)。

    规则 19 允许的偏离:问题通顺地点名了某列(如 "districts")而 plan
    没写进 answer_columns 时,结果里带出该列不是错误——豁免之。
    """
    tail = column.split(".", 1)[-1].lower()
    for candidate in (tail, tail.replace("_", " ")):
        if re.search(rf"\b{re.escape(candidate)}s?\b", question_lower):
            return True
    return False


def extra_columns_mismatch(
    plan_json: dict[str, Any] | None,
    result_columns: list[str],
    question: str,
) -> list[str]:
    """plan 的 answer_columns 与执行结果列的"多余列"检查(层2补充,确定性)。

    与 answer_columns_mismatch 互补:那个查"答案列全缺",这个查
    "结果列多余"。保守方向(宁漏勿误):
    - 前置条件:所有直接引用都出现在结果列中——任一缺失留给层2主检查,
      避免双重打回;
    - 豁免:结果列与 answer ref 大小写不敏感匹配(含去表限定尾缀);
      列名以单词形式出现在 question 文本中(规则 19 允许的偏离)。
    剩余多余列 → 冲突。误伤成本 = 一次共享预算重试轮。
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
    for r in refs:
        if r.lower() not in lower_result and r.split(".", 1)[-1].lower() not in lower_result:
            return []  # 有答案列缺失 → 交给层2主检查(宁漏勿误)
    ref_tails = {r.split(".", 1)[-1].lower() for r in refs}
    q_lower = (question or "").lower()
    extra = [
        c for c in result_columns
        if c.lower() not in ref_tails
        and not _word_in_question(c, q_lower)
    ]
    if not extra:
        return []
    return [
        f"result columns {list(extra)} are not in the plan's answer_columns {refs} "
        "— output only the answer columns"
    ]


async def _schema_map(connectors, datasource: str | None = None) -> dict[str, set[str]] | None:
    """真实 schema → 小写表名 → 小写列名集合;不可用 → None(跳过校验)。"""
    if connectors is None:
        return None
    try:
        schema = await connectors.get_schema(datasource)
        return {
            t.name.lower(): {c.name.lower() for c in t.columns}
            for t in schema.tables
        }
    except Exception:
        return None


def _short_value(v: Any) -> str:
    """观测里的单值:截断为短字符串。"""
    if v is None:
        return "null"
    s = str(v)
    return s[:40] + "…" if len(s) > 40 else s


async def _column_stats_text(
    connectors, table: str, column: str, datasource: str | None = None,
) -> str:
    """列画像观测:行数 / null 比例 / distinct / 样例 / 低基数高频值。

    运行时探测,**永不抛异常**——失败折叠成短错误文本。方言感知引号
    (schema_linking.py JOIN_PROBE 同款惯例):MySQL 反引号,其余双引号
    (SQLite 接受双引号标识符)。每个探测独立 5s 超时、失败静默跳过。
    高基数列(>30 distinct)不展示 top 值——top 只对低基数列有意义。
    """
    if connectors is None:
        return "error: no datasource available"
    try:
        adapter = await connectors.get(datasource)
        quote = "`" if adapter.dialect() == "mysql" else '"'
    except Exception as e:
        return f"error: {e}"
    t, c = str(table or "").strip(), str(column or "").strip()
    if not t or not c:
        return "error: both table and column are required"

    # 表/列存在性(schema 可用时;不可用则靠探查询的 SQL 错误兜底)
    distinct: int | None = None
    try:
        schema = await asyncio.wait_for(connectors.get_schema(datasource), timeout=5.0)
        tbl = next((x for x in schema.tables if x.name.lower() == t.lower()), None)
        if tbl is None:
            return f"table '{t}' not found"
        if c.lower() not in {col.name.lower() for col in tbl.columns}:
            return f"column '{c}' not found in table '{t}'"
    except Exception:
        pass

    q_t, q_c = f"{quote}{t}{quote}", f"{quote}{c}{quote}"

    agg: list | None = None
    try:
        r = await asyncio.wait_for(connectors.execute(
            f"SELECT COUNT(*), SUM({q_c} IS NULL), COUNT(DISTINCT {q_c}) FROM {q_t}",
            datasource,
        ), timeout=5.0)
        if r.rows and r.rows[0]:
            agg = r.rows[0]
            distinct = agg[2]
    except Exception:
        pass

    sample: list[str] = []
    try:
        r = await asyncio.wait_for(connectors.execute(
            f"SELECT DISTINCT {q_c} FROM {q_t} WHERE {q_c} IS NOT NULL LIMIT 5",
            datasource,
        ), timeout=5.0)
        sample = [_short_value(row[0]) for row in (r.rows or [])[:5]]
    except Exception:
        pass

    top: list[tuple[str, Any]] = []
    if distinct is None or 2 <= distinct <= 30:
        try:
            r = await asyncio.wait_for(connectors.execute(
                f"SELECT {q_c}, COUNT(*) FROM {q_t} GROUP BY {q_c} "
                f"ORDER BY COUNT(*) DESC, {q_c} LIMIT 10",
                datasource,
            ), timeout=5.0)
            top = [(_short_value(row[0]), row[1]) for row in (r.rows or [])[:10]]
        except Exception:
            pass

    parts: list[str] = []
    if agg is not None:
        rows, nulls = agg[0], agg[1] or 0
        null_ratio = round(nulls / rows, 3) if rows else 0.0
        parts.append(f"rows={rows} null_ratio={null_ratio} distinct={distinct}")
    if sample:
        parts.append("sample: " + ", ".join(sample))
    if top:
        parts.append("top: " + ", ".join(f"{v} ({n})" for v, n in top))
    if not parts:
        return f"error: no stats available for {t}.{c}"
    return "; ".join(parts)


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
        schema_map = await _schema_map(connectors, state.datasource or None)
        # 计划起草走 fast 档(未配置 fast → 回退 target)
        model = config.model_fast or config.target or "openai/gpt-4o"
        system_prompt = render(
            "planner/system",
            lang=state.lang,
            has_tools=bool(agentic and connectors is not None),
        )
        # 方法论 skill:按节点确定性匹配(manifest.yml),注入 system prompt
        skill_block = render_skills("planner", lang=state.lang)
        if skill_block:
            system_prompt = f"{system_prompt}\n\n{skill_block}"
        llm_detail: dict[str, Any] | None = None
        trail = ""

        async def call_planner(correction: str) -> str:
            nonlocal llm_detail, trail
            prompt = render(
                "planner/user",
                lang=state.lang,
                question=state.question,
                schema_context=state.schema_context[:10000],
                evidence=state.evidence,
                time_context=state.time_context,
                history=state.history,
                correction=correction[:600] if correction else "",
                previous_plan=state.plan[:800] if state.plan else "",
            )
            if agentic and connectors is not None:
                from trove.llm.agent_loop import ToolRegistry

                registry = ToolRegistry(finish=True)

                async def table_columns(arguments: dict) -> str:
                    table = arguments.get("table", "")
                    schema = await connectors.get_schema(state.datasource or None)
                    for t in schema.tables:
                        if t.name.lower() == table.lower():
                            return ", ".join(f"{c.name} {c.type}" for c in t.columns)
                    return f"table '{table}' not found"

                async def column_stats(arguments: dict) -> str:
                    # 列画像:起草条件前锚定过滤值的真实取值与行数量级
                    return await _column_stats_text(
                        connectors,
                        arguments.get("table", ""),
                        arguments.get("column", ""),
                        state.datasource or None,
                    )

                registry.register(
                    "get_table_columns", table_columns,
                    description="Inspect the columns of one table.",
                    parameters={
                        "type": "object",
                        "properties": {"table": {"type": "string"}},
                        "required": ["table"],
                    },
                )
                registry.register(
                    "get_column_stats", column_stats,
                    description=(
                        "Inspect a column's real data: row count, null ratio, "
                        "distinct count, sample values, and (for low-cardinality "
                        "columns) the most frequent values with counts. "
                        "Use BEFORE drafting filter conditions to anchor values "
                        "to actual data — what type/frequency/status columns "
                        "really store, and the row-count scale of a table."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "table": {"type": "string"},
                            "column": {"type": "string"},
                        },
                        "required": ["table", "column"],
                    },
                )

                result = await run_agent_loop(
                    llm, model,
                    system=system_prompt,
                    user=prompt,
                    registry=registry,
                    tool_timeout_s=20.0,
                    time_budget_s=60.0,
                    max_rounds=3,
                    max_total_tokens=1500,
                    metadata={"node": "planner", "session_id": state.session_id, "run_id": state.run_id},
                )
                t = " ".join(
                    p for p in (result.get("reasoning", ""), result.get("transcript", "")) if p
                )
                trail = t[:800]
                if not result.get("guard_hit"):
                    return result["content"]
                # 护栏降级:agent loop 原地打转/预算耗尽 → 退到直接生成
                # (plan 校验在 call_planner 之外,照常拦截幻觉列)
                logger.warning(
                    "Planner agent loop guard (%s, %d rounds); degrading to direct generation",
                    result.get("budget_why"), result["rounds"],
                )

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
