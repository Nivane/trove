"""Attribution node — business-level "why / root cause" drill-down.

Answers why-questions ("为什么营收下降"、"哪个地区贡献最大") on top of the
plain retrieval pipeline. Runs AFTER the main query passes reflect (the
headline metric is already retrieved); it is an enhancement layer that
degrades silently, never blocks the main answer.

Multi-hop drill-down (max_hops config, v1 default 2):
  - hop0: overall delta — target metric current period vs base period
  - hop1: dimension breakdown — group by dimensions[0], per-item delta
  - hop2: drill into the top |contribution| item, group by dimensions[1]

Contribution math is deterministic pure code (`_contribution`, zero LLM):
  delta_i = cur_i - base_i; total = Σ|delta_i|; contribution_i = delta_i/total.
  When total == 0 → falls back to share attribution (cur_i / Σcur_i).
Only the narrative is LLM-generated, and it is grounded in the contribution
table (prompt hard-constrains: cite table numbers only, never invent).

Node shape: `make_attribution(llm, config, connectors, semantic_layer)
-> async def attribution(state) -> dict` returns a partial state update.
Passes through (returns {}) when attribution is disabled, no attribution
plan, no connectors/semantic layer, or any hop fails (degrade to the hops
that succeeded).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.prompts import render
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

MAX_ATTRIBUTION_ROWS = 50  # 归因表注入叙事 prompt 的行数上限


# ── 确定性贡献率计算(纯函数,零 LLM,可单测)────────────────

def _num(value: Any) -> float:
    """容忍数值解析:None/空 → 0.0;字符串去 %/逗号/货币符号。"""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    s = s.replace(",", "").replace("，", "").lstrip("¥$€£￥")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _contribution(
    base_map: dict[str, float],
    cur_map: dict[str, float],
) -> list[dict[str, Any]]:
    """维度贡献率表(确定性)。

    - delta_i = cur_i - base_i(缺失维度按 0 计);
    - total_abs = Σ|delta_i|;contribution_i = delta_i / total_abs(带符号);
    - total_abs == 0(无变化)→ 退化为占比归因 cur_i / Σcur_i;
    - 结果按 |contribution| 降序(top 贡献者在前,下钻用)。

    Returns: [{"dim", "base", "current", "delta", "contribution"}, ...]
    """
    keys = set(base_map) | set(cur_map)
    items: list[dict[str, Any]] = []
    for k in keys:
        b = _num(base_map.get(k, 0.0))
        c = _num(cur_map.get(k, 0.0))
        items.append({"dim": k, "base": b, "current": c, "delta": c - b})
    total_abs = sum(abs(it["delta"]) for it in items)
    if total_abs == 0:
        total_cur = sum(it["current"] for it in items) or 1.0
        for it in items:
            it["contribution"] = it["current"] / total_cur
    else:
        for it in items:
            it["contribution"] = it["delta"] / total_abs
    items.sort(key=lambda x: abs(x["contribution"]), reverse=True)
    return items


def _base_period(
    time_context: str,
    baseline: str,
) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """当前期 + 基期(从 parse_date 的 time_context 确定性派生)。

    time_context: "YYYY-MM-DD ~ YYYY-MM-DD"。基期派生:
      - prev_period:往前推一个等长窗口(环比);
      - yoy:往前推 1 年(同比,月/日钳制);
      - share:无基期(占比归因,返回 None)。
    格式非法/无时间 → None(调用方降级 share)。
    """
    import re

    m = re.match(r"^(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})$", (time_context or "").strip())
    if not m:
        return None
    try:
        start = date.fromisoformat(m.group(1))
        end = date.fromisoformat(m.group(2))
    except ValueError:
        return None
    if baseline == "share":
        return None
    if baseline == "yoy":
        base_start = _shift_months(start, -12)
        base_end = _shift_months(end, -12)
    else:  # prev_period
        span = (end - start).days + 1
        base_end = start - timedelta(days=1)
        base_start = base_end - timedelta(days=span - 1)
    return (
        (start.isoformat(), end.isoformat()),
        (base_start.isoformat(), base_end.isoformat()),
    )


def _shift_months(d: date, n: int) -> date:
    """d + n 个月,日钳制到目标月长度(与 parse_date 同款)。"""
    import calendar

    month_index = d.year * 12 + (d.month - 1) + n
    year, month = divmod(month_index, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _waterfall_chart(
    question: str,
    baseline_label: str,
    base_total: float,
    cur_total: float,
    table: list[dict[str, Any]],
    lang: str,
) -> dict[str, Any] | None:
    """归因表 → ECharts 瀑布图 payload。

    categories: [基期, 各维度项…, 当前];series.data = [base_total, delta…,
    cur_total]。前端 ECharts 渲染;CLI 由 spark.render_waterfall_ascii 兜底。
    无维度项 → None(没有可拆的瀑布)。
    """
    zh = lang == "zh"
    dims = [str(it["dim"]) for it in table]
    if not dims:
        return None
    categories = [baseline_label] + dims + [
        ("当前" if zh else "Current"),
    ]
    data = [base_total] + [it["delta"] for it in table] + [cur_total]
    return {
        "type": "waterfall",
        "title": (question or "").strip()[:60],
        "dimension": ("维度贡献" if zh else "dimension contribution"),
        "categories": categories,
        "series": [{"name": ("Δ" if zh else "delta"), "data": data}],
        "measures": ["delta"],
    }


# ── SQL 构造(复用语义编译器:hops 走确定性编译,失败即降级)────────

def _compile_hop(
    semantic_layer: Any,
    matched: list[str],
    dialect: str,
    metric_name: str,
    dim_refs: list[str],
    conds: list[dict[str, Any]],
) -> str | None:
    """构造并编译一跳查询 → SQL(编译 MISS → None,调用方降级)。

    plan.aggregation 填度量名(编译器按名解析);answer_columns 前段为
    维度字段(非聚合列 → GROUP BY),末段为度量名(裸名 → 度量投影)。
    """
    try:
        from trove.services.semantic_layer.compiler import CompileMiss, SemanticCompiler

        model = semantic_layer.model()
        if model is None:
            return None
        compiler = SemanticCompiler(model)
        metric = compiler._metric_by_name(metric_name)
        if metric is None:
            return None
        plan = {
            "tables": list(matched),
            "aggregation": metric_name,
            "answer_columns": list(dim_refs) + [metric_name],
            "conditions": conds,
        }
        result = compiler.compile_detailed(plan, list(matched), force_dialect=dialect)
        if isinstance(result, CompileMiss):
            return None
        return result.sql
    except Exception as e:
        logger.warning("Attribution hop compile failed: %s", e)
        return None


def _resolve_time_field(semantic_layer: Any, matched: list[str], metric_name: str) -> str | None:
    """度量锚定的声明时间字段引用(dataset.field);不可判定 → None。"""
    try:
        from trove.services.semantic_layer.compiler import SemanticCompiler, resolve_time_field

        model = semantic_layer.model()
        if model is None:
            return None
        compiler = SemanticCompiler(model)
        metric = compiler._metric_by_name(metric_name)
        preferred = metric.agg_time_dimension if metric is not None else ""
        resolved = resolve_time_field(model, list(matched), preferred=preferred)
        if resolved is None:
            return None
        return f"{resolved[0]}.{resolved[1].name}"
    except Exception:
        return None


def _resolve_dim_ref(semantic_layer: Any, matched: list[str], dim: str) -> str | None:
    """维度名 → 声明字段引用(dataset.field);不可解析 → None。"""
    try:
        from trove.services.semantic_layer.compiler import SemanticCompiler

        model = semantic_layer.model()
        if model is None:
            return None
        compiler = SemanticCompiler(model)
        resolved = compiler._resolve_field(str(dim).strip(), set(matched))
        if resolved is None:
            return None
        return f"{resolved[0]}.{resolved[1].name}"
    except Exception:
        return None


def _time_conds(time_field: str, period: tuple[str, str] | None) -> list[dict[str, Any]]:
    """时间范围 → plan conditions(半开区间用 >=/< 表达;period None → 空)。"""
    if not time_field or period is None:
        return []
    start, end = period
    return [
        {"field": time_field, "op": ">=", "value": start, "note": "attribution period start"},
        {"field": time_field, "op": "<=", "value": end, "note": "attribution period end"},
    ]


async def _run_hop(
    connectors: Any,
    sql: str,
    datasource: str,
    timeout_s: float = 5.0,
) -> tuple[list[str], list[list[Any]]]:
    """只读执行一跳(复用 connectors.execute 的只读守卫);超时/失败抛错。"""
    result = await asyncio.wait_for(
        connectors.execute(sql, datasource or None),
        timeout=timeout_s,
    )
    return list(result.columns), list(result.rows)


def _rows_to_map(columns: list[str], rows: list[list[Any]]) -> dict[str, float]:
    """hop 结果(维度, 度量)→ {dim: value}。首列为维度,末列为度量。"""
    out: dict[str, float] = {}
    if not columns or not rows:
        return out
    for row in rows:
        if len(row) < 2:
            continue
        out[str(row[0])] = _num(row[-1])
    return out


# ── 节点 ─────────────────────────────────────────────────

def make_attribution(
    llm: LLMGateway,
    config: AgentConfig,
    connectors=None,
    semantic_layer=None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the attribution node bound to services.

    Args:
        llm: LLM gateway (narrative generation; grounded in the table).
        config: AgentConfig (attribution.enabled / max_hops / node model).
        connectors: ConnectorRegistry used to run hop queries (None → skip).
        semantic_layer: Live semantic provider (metrics/dimensions/time).
    """

    async def attribution(state: WorkflowState) -> dict[str, Any]:
        if state.error or not state.attribution_plan:
            return {}
        if not config.attribution.enabled:
            return {}
        if connectors is None or semantic_layer is None:
            return {}
        if not state.matched_tables:
            return {}

        plan = state.attribution_plan
        metric_name = str(plan.get("target_metric") or "").strip()
        dims = [
            str(d).strip()
            for d in (plan.get("dimensions") or [])
            if str(d or "").strip()
        ][: config.attribution.max_dimensions]
        if not metric_name or not dims:
            return {}
        baseline = str(plan.get("baseline") or "prev_period").strip().lower()
        if baseline not in ("prev_period", "yoy", "share"):
            baseline = "prev_period"
        depth = min(int(plan.get("depth") or 1), config.attribution.max_hops)
        focus = plan.get("focus")

        dialect = state.dialect or "sqlite"
        matched = list(state.matched_tables)
        hops: list[dict[str, Any]] = []
        result: dict[str, Any] = {}

        # 时间字段判定失败 → baseline 降级 share(无基期,占比归因)
        time_field = _resolve_time_field(semantic_layer, matched, metric_name)
        periods = None
        if time_field:
            periods = _base_period(state.time_context, baseline)
        if periods is None:
            baseline = "share"
        cur_period = periods[0] if periods else None
        base_period = periods[1] if periods else None

        # 维度字段解析(全部解析失败 → 静默跳过,不给编造维度)
        dim_refs: list[str] = []
        for d in dims:
            ref = _resolve_dim_ref(semantic_layer, matched, d)
            if ref is None:
                break
            dim_refs.append(ref)
        if not dim_refs:
            return {}

        try:
            # hop0:整体 Δ(无维度)——当前期 vs 基期总量对比
            cur_sql = _compile_hop(
                semantic_layer, matched, dialect, metric_name, [], _time_conds(time_field, cur_period)
            )
            base_sql = _compile_hop(
                semantic_layer, matched, dialect, metric_name, [], _time_conds(time_field, base_period)
            )
            cur_total = base_total = 0.0
            if cur_sql:
                cols, rows = await _run_hop(connectors, cur_sql, state.datasource)
                cur_total = _num(rows[0][-1]) if rows and rows[0] else 0.0
                hops.append({"hop": 0, "sql": cur_sql, "columns": cols, "rows": rows[:5], "period": "current"})
            if base_sql and base_period:
                cols, rows = await _run_hop(connectors, base_sql, state.datasource)
                base_total = _num(rows[0][-1]) if rows and rows[0] else 0.0
                hops.append({"hop": 0, "sql": base_sql, "columns": cols, "rows": rows[:5], "period": "base"})
            total_delta = cur_total - base_total

            # hop1:按 dimensions[0] 分解
            d0_ref = dim_refs[0]
            focus_conds = (
                [{"field": d0_ref, "op": "=", "value": focus}]
                if focus else []
            )
            cur_sql = _compile_hop(
                semantic_layer, matched, dialect, metric_name, [d0_ref],
                focus_conds + _time_conds(time_field, cur_period),
            )
            base_sql = _compile_hop(
                semantic_layer, matched, dialect, metric_name, [d0_ref],
                focus_conds + _time_conds(time_field, base_period),
            )
            cur_map: dict[str, float] = {}
            base_map: dict[str, float] = {}
            if cur_sql:
                cols, rows = await _run_hop(connectors, cur_sql, state.datasource)
                cur_map = _rows_to_map(cols, rows)
                hops.append({"hop": 1, "sql": cur_sql, "columns": cols, "rows": rows[:10], "period": "current"})
            if base_sql and base_period:
                cols, rows = await _run_hop(connectors, base_sql, state.datasource)
                base_map = _rows_to_map(cols, rows)
                hops.append({"hop": 1, "sql": base_sql, "columns": cols, "rows": rows[:10], "period": "base"})
            table = _contribution(base_map, cur_map)

            # hop2:下钻(depth>=2 且还有第二个维度)——对 top |contribution|
            # 项加过滤后按 dimensions[1] 再分解。
            if depth >= 2 and len(dim_refs) >= 2:
                top = table[0] if table else None
                if top and top["delta"] != 0:
                    d1_ref = dim_refs[1]
                    drill_conds = [{"field": d0_ref, "op": "=", "value": str(top["dim"])}]
                    cur_sql = _compile_hop(
                        semantic_layer, matched, dialect, metric_name, [d1_ref],
                        drill_conds + _time_conds(time_field, cur_period),
                    )
                    base_sql = _compile_hop(
                        semantic_layer, matched, dialect, metric_name, [d1_ref],
                        drill_conds + _time_conds(time_field, base_period),
                    )
                    drill_cur: dict[str, float] = {}
                    drill_base: dict[str, float] = {}
                    if cur_sql:
                        cols, rows = await _run_hop(connectors, cur_sql, state.datasource)
                        drill_cur = _rows_to_map(cols, rows)
                        hops.append({"hop": 2, "sql": cur_sql, "columns": cols, "rows": rows[:10], "period": "current", "filter": str(top["dim"])})
                    if base_sql and base_period:
                        cols, rows = await _run_hop(connectors, base_sql, state.datasource)
                        drill_base = _rows_to_map(cols, rows)
                        hops.append({"hop": 2, "sql": base_sql, "columns": cols, "rows": rows[:10], "period": "base", "filter": str(top["dim"])})
                    drill_table = _contribution(drill_base, drill_cur)
                else:
                    drill_table = []
            else:
                drill_table = []

            # 归因叙事(LLM,ground 在归因表;走 node_models["attribution"]
            # 覆盖,缺省回落 model_for → model_fast,与 insights 一致)。
            narrative = ""
            table_text = "\n".join(
                f"{it['dim']}\t{it['base']:g}\t{it['current']:g}\t{it['delta']:g}\t{it['contribution']:+.1%}"
                for it in table[:MAX_ATTRIBUTION_ROWS]
            )
            if llm is not None and table_text:
                model = config.model_for_node("attribution", state.complexity)
                baseline_label = {
                    "prev_period": "环比" if state.lang == "zh" else "previous period",
                    "yoy": "同比" if state.lang == "zh" else "year-over-year",
                    "share": "占比" if state.lang == "zh" else "share",
                }[baseline]
                try:
                    start = time.monotonic()
                    response = await llm.chat(
                        model=model,
                        messages=[
                            {"role": "system", "content": render("attribution/system", lang=state.lang)},
                            {"role": "user", "content": render(
                                "attribution/user",
                                lang=state.lang,
                                question=state.question,
                                metric=metric_name,
                                dimension=dims[0],
                                baseline=baseline_label,
                                total_delta=total_delta,
                                table=table_text,
                            )},
                        ],
                        max_tokens=16000,
                        metadata={
                            "node": "attribution",
                            "session_id": state.session_id,
                            "run_id": state.run_id,
                            "question": state.question[:80],
                        },
                    )
                    narrative = (response or "").strip()
                except Exception as e:
                    logger.warning("Attribution narrative failed (%s); skipping", e)

            zh = state.lang == "zh"
            baseline_label = {
                "prev_period": "基期" if zh else "Base period",
                "yoy": "去年同期" if zh else "Same period last year",
                "share": "本期" if zh else "Current period",
            }[baseline]
            chart = _waterfall_chart(
                state.question, baseline_label, base_total, cur_total, table, state.lang,
            )

            result = {
                "total_delta": total_delta,
                "table": table,
                "narrative": narrative,
                "hops": hops,
                "dimensions": dims,
                "baseline": baseline,
                "chart": chart,
                "metric": metric_name,
            }
            # 下钻表并入(有则挂到 result,供前端分析面板/后续洞察复用)
            if drill_table:
                result["drilldown"] = {
                    "dimension": dims[1] if len(dims) > 1 else dims[0],
                    "table": drill_table,
                }
        except Exception as e:
            logger.warning("Attribution analysis failed (%s); degrading to partial hops", e)
            if not result and hops:
                result = {"table": [], "hops": hops, "narrative": "", "baseline": baseline}

        if not result:
            return {}
        # 一跳都没执行成功(指标/维度/时间全不可解析)→ 视为未生成,静默跳过
        if not hops:
            return {}
        return {"attribution": result, "attribution_hops": hops}

    return attribution
