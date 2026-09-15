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


# ── SQL 构造(复用语义编译器:hops 由编译器直接产出,失败即降级)────────

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
        from trove.services.semantic_layer.compiler import (
            CompileResult,
            SemanticCompiler,
        )

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
        # 归因下钻需要完整编译(软 MISS 骨架不算——跳一跳不能建立在缺组件上)
        if not isinstance(result, CompileResult):
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


def _rows_to_numden(columns: list[str], rows: list[list[Any]]) -> dict[str, tuple[float, float]]:
    """比率 hop 结果(维度, 分子, 分母)→ {dim: (num, den)}。"""
    out: dict[str, tuple[float, float]] = {}
    if not columns or not rows:
        return out
    for row in rows:
        if len(row) < 3:
            continue
        out[str(row[0])] = (_num(row[1]), _num(row[2]))
    return out


# ── 比率指标 shift-share 分解(纯函数,零 LLM)────────────────

def _metric_ratio_parts(metric: Any) -> tuple[str, str] | None:
    """比率度量 → (分子, 分母) 聚合表达式;加性/不可分解 → None。

    识别:metric_type == ratio,或表达式是 AVG(x)(→ SUM(x)/COUNT(x))、
    A/B 除法、SAFE_DIVIDE(A, B)。Paren 解包后判定。表达式为表限定
    (``students.grade``),产物可直接拼进 hop SQL。
    """
    if metric is None:
        return None
    expr = str(getattr(metric, "expression", "") or "").strip()
    if not expr:
        return None
    try:
        from sqlglot import exp, parse_one
        tree = parse_one(expr)
        while isinstance(tree, exp.Paren):
            tree = tree.this
        if isinstance(tree, exp.Avg):
            arg = tree.this.sql()
            return f"SUM({arg})", f"COUNT({arg})"
        if isinstance(tree, exp.Div):
            return tree.this.sql(), tree.expression.sql()
        if isinstance(tree, exp.SafeDivide):
            return tree.this.sql(), tree.expression.sql()
    except Exception:
        return None
    return None


def _compile_ratio_hop(
    semantic_layer: Any,
    matched: list[str],
    dialect: str,
    metric: Any,
    ratio_parts: tuple[str, str],
    dim_ref: str,
    conds: list[dict[str, Any]],
) -> str | None:
    """构造比率 hop SQL:SELECT dim, num, den FROM <单数据集> WHERE ... GROUP BY dim。

    只支持单数据集度量(比率分子/分母同表,无需 join);多数据集 → None
    (调用方降级加性路径)。条件字段已是表限定,字面量用编译器 _literal。
    """
    try:
        from trove.services.semantic_layer.compiler import _literal
        num_sql, den_sql = ratio_parts
        ds = [d for d in (getattr(metric, "datasets", None) or []) if d]
        if len(set(ds)) != 1:
            return None
        table = ds[0]
        if table not in {str(t) for t in (matched or [])}:
            return None
        # 维度必须落在度量所在数据集:比率 hop 无 join,FROM 单表即唯一
        # 数据来源,跨表维度无法在同一个 GROUP BY 里解析。
        dim_tbl = str(dim_ref or "").split(".", 1)[0]
        if dim_tbl and dim_tbl != table:
            return None
        where = ""
        if conds:
            parts = []
            for c in conds:
                if not isinstance(c, dict):
                    continue
                field = str(c.get("field") or "").strip()
                op = str(c.get("op") or "=").strip()
                value = c.get("value")
                if not field or value is None:
                    continue
                parts.append(f"{field} {op} {_literal(value)}")
            if parts:
                where = " WHERE " + " AND ".join(parts)
        return f"SELECT {dim_ref}, {num_sql} AS __num, {den_sql} AS __den FROM {table}{where} GROUP BY {dim_ref}"
    except Exception:
        return None


def _shift_share(base_nd: dict[str, tuple[float, float]], cur_nd: dict[str, tuple[float, float]]) -> dict[str, Any]:
    """比率指标变化的 shift-share 分解(精确恒等式)。

    R = Σ_i rate_i * weight_i,其中 rate_i = n_i/d_i,weight_i = d_i/Σd。
    ΔR = R_cur − R_base 拆成三部分,每项按组可加、总和精确等于 ΔR:
      - within(本征/率效应):Σ w_base_i * (rate_cur_i − rate_base_i)
      - composition(结构/混合效应):Σ (w_cur_i − w_base_i) * rate_base_i
      - interaction(交叉项):Σ (w_cur_i − w_base_i) * (rate_cur_i − rate_base_i)
    每组的 contribution_i = w_cur*r_cur − w_base*r_base(= 三效应之和)。

    Returns: {rows, effects, base_total, cur_total}。rows 按 |contribution|
    降序,含 base_rate/current_rate/base_weight/current_weight/within/
    composition/interaction/contribution;effects 为三类效应总和 + ΔR。
    """
    dims = set(base_nd) | set(cur_nd)
    D_base = sum(d for _, d in base_nd.values())
    D_cur = sum(d for _, d in cur_nd.values())
    N_base = sum(n for n, _ in base_nd.values())
    N_cur = sum(n for n, _ in cur_nd.values())
    R_base = N_base / D_base if D_base else 0.0
    R_cur = N_cur / D_cur if D_cur else 0.0
    rows: list[dict[str, Any]] = []
    for k in dims:
        n_b, d_b = base_nd.get(k, (0.0, 0.0))
        n_c, d_c = cur_nd.get(k, (0.0, 0.0))
        r_b = n_b / d_b if d_b else 0.0
        r_c = n_c / d_c if d_c else 0.0
        w_b = d_b / D_base if D_base else 0.0
        w_c = d_c / D_cur if D_cur else 0.0
        within = w_b * (r_c - r_b)
        composition = (w_c - w_b) * r_b
        interaction = (w_c - w_b) * (r_c - r_b)
        contribution = within + composition + interaction
        rows.append({
            "dim": k,
            "base": r_b, "current": r_c, "delta": r_c - r_b,
            "base_rate": r_b, "current_rate": r_c,
            "base_weight": w_b, "current_weight": w_c,
            "within": within, "composition": composition, "interaction": interaction,
            "contribution": contribution,
        })
    rows.sort(key=lambda r: abs(r["contribution"]), reverse=True)
    effects = {
        "within": sum(r["within"] for r in rows),
        "composition": sum(r["composition"] for r in rows),
        "interaction": sum(r["interaction"] for r in rows),
        "delta": R_cur - R_base,
        "base_rate": R_base, "current_rate": R_cur,
    }
    return {"rows": rows, "effects": effects, "base_total": R_base, "cur_total": R_cur}


def _ratio_share(cur_nd: dict[str, tuple[float, float]]) -> dict[str, Any]:
    """比率指标占比归因(share 基线,无基期):贡献 = 分子份额 n_i/N。"""
    N = sum(n for n, _ in cur_nd.values())
    D = sum(d for _, d in cur_nd.values())
    R = N / D if D else 0.0
    rows: list[dict[str, Any]] = []
    for k, (n, d) in cur_nd.items():
        rate = n / d if d else 0.0
        weight = d / D if D else 0.0
        rows.append({
            "dim": k,
            "base": rate, "current": rate, "delta": rate,
            "base_rate": rate, "current_rate": rate,
            "base_weight": weight, "current_weight": weight,
            "within": 0.0, "composition": 0.0, "interaction": 0.0,
            "contribution": (n / N) if N else 0.0,
        })
    rows.sort(key=lambda r: abs(r["contribution"]), reverse=True)
    return {"rows": rows, "effects": None, "base_total": 0.0, "cur_total": R}


def _ratio_waterfall_chart(
    question: str,
    lang: str,
    base_rate: float,
    effects: dict[str, float],
    cur_rate: float,
) -> dict[str, Any]:
    """比率分解瀑布图:基期率 → 本征 → 结构 → 交叉 → 当前率。"""
    if not effects:
        return None
    zh = lang == "zh"
    labels = (["基期", "本征效应", "结构效应", "交叉效应", "当前"]
              if zh else ["Base", "Within", "Composition", "Interaction", "Current"])
    return {
        "type": "waterfall",
        "title": (question or "").strip()[:60],
        "dimension": ("率分解" if zh else "rate decomposition"),
        "categories": labels,
        "series": [{
            "name": "Δ" if zh else "delta",
            "data": [
                base_rate,
                effects["within"],
                effects["composition"],
                effects["interaction"],
                cur_rate,
            ],
        }],
        "measures": ["delta"],
    }


def _breakdown_signal(cur_map: dict[str, Any], base_map: dict[str, Any], ratio_parts: tuple[str, str] | None) -> float:
    """维度解释力信号:Σ|per-group Δ|(加性=值变化,比率=分解贡献)。

    加性:Σ|cur_i − base_i|(与 _contribution 的 total_abs 一致);比率:
    shift-share 分解后 Σ|contribution_i|(各组对整体率变化的贡献绝对值)。
    """
    if not cur_map and not base_map:
        return 0.0
    if ratio_parts:
        dec = _shift_share(
            {k: tuple(map(float, v)) for k, v in (base_map or {}).items()},
            {k: tuple(map(float, v)) for k, v in (cur_map or {}).items()},
        )
        return sum(abs(r["contribution"]) for r in dec["rows"])
    keys = set(cur_map) | set(base_map)
    return sum(abs(float(cur_map.get(k, 0.0)) - float(base_map.get(k, 0.0))) for k in keys)


def _resolve_metric(semantic_layer: Any, metric_name: str) -> Any:
    """语义模型里的目标度量对象;解析失败 → None。"""
    try:
        from trove.services.semantic_layer.compiler import SemanticCompiler

        model = semantic_layer.model()
        if model is None:
            return None
        return SemanticCompiler(model)._metric_by_name(metric_name)
    except Exception:
        return None


async def _probe_dim(
    connectors: Any,
    datasource: str,
    semantic_layer: Any,
    matched: list[str],
    dialect: str,
    metric_name: str,
    metric: Any,
    ratio_parts: tuple[str, str] | None,
    dim_ref: str,
    conds_extra: list[dict[str, Any]],
    time_field: str | None,
    cur_period: tuple[str, str] | None,
    base_period: tuple[str, str] | None,
) -> tuple[dict[str, Any], dict[str, Any], float, dict[str, Any] | None, dict[str, Any] | None]:
    """一个候选维的双期 GROUP BY 探测 → (cur_map, base_map, signal, hop_cur, hop_base)。

    hop_cur/hop_base: 成功执行时该跳的观测条目(记录用),失败 → None。
    比率度量用 num/den 双列;加性用单值列。
    """
    hop_cur = hop_base = None
    if ratio_parts is not None and metric is not None:
        cur_sql = _compile_ratio_hop(
            semantic_layer, matched, dialect, metric, ratio_parts,
            dim_ref, conds_extra + _time_conds(time_field, cur_period),
        )
        base_sql = (
            _compile_ratio_hop(
                semantic_layer, matched, dialect, metric, ratio_parts,
                dim_ref, conds_extra + _time_conds(time_field, base_period),
            )
            if base_period else None
        )
        cur_map: dict[str, Any] = {}
        base_map: dict[str, Any] = {}
        if cur_sql:
            cols, rows = await _run_hop(connectors, cur_sql, datasource)
            cur_map = _rows_to_numden(cols, rows)
            hop_cur = {"hop": 1, "sql": cur_sql, "columns": cols, "rows": rows[:10], "period": "current"}
        if base_sql:
            cols, rows = await _run_hop(connectors, base_sql, datasource)
            base_map = _rows_to_numden(cols, rows)
            hop_base = {"hop": 1, "sql": base_sql, "columns": cols, "rows": rows[:10], "period": "base"}
    else:
        cur_sql = _compile_hop(
            semantic_layer, matched, dialect, metric_name, [dim_ref],
            conds_extra + _time_conds(time_field, cur_period),
        )
        base_sql = (
            _compile_hop(
                semantic_layer, matched, dialect, metric_name, [dim_ref],
                conds_extra + _time_conds(time_field, base_period),
            )
            if base_period else None
        )
        cur_map = {}
        base_map = {}
        if cur_sql:
            cols, rows = await _run_hop(connectors, cur_sql, datasource)
            cur_map = _rows_to_map(cols, rows)
            hop_cur = {"hop": 1, "sql": cur_sql, "columns": cols, "rows": rows[:10], "period": "current"}
        if base_sql:
            cols, rows = await _run_hop(connectors, base_sql, datasource)
            base_map = _rows_to_map(cols, rows)
            hop_base = {"hop": 1, "sql": base_sql, "columns": cols, "rows": rows[:10], "period": "base"}
    signal = _breakdown_signal(cur_map, base_map, ratio_parts)
    return cur_map, base_map, signal, hop_cur, hop_base


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
        # 计划维度名 ↔ 解析 ref 的映射(ref 可被 probe 重排,名字不可)
        ref_to_dim: dict[str, str] = dict(zip(dim_refs, dims))

        # 度量解析 + 比率判定(方向 2):metric_type=ratio / AVG / A÷B / SAFE_DIVIDE
        metric_obj = _resolve_metric(semantic_layer, metric_name)
        ratio_parts: tuple[str, str] | None = None
        if config.attribution.ratio_decomposition and metric_obj is not None:
            ratio_parts = _metric_ratio_parts(metric_obj)
        is_ratio = ratio_parts is not None

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

            # focus 属于计划的首维(问题里点名的那一项),探测时排除(要
            # 的是全量分组的信号,不是单值退化);hop1 时再套上。
            focus_dim_ref = dim_refs[0]
            focus_conds = (
                [{"field": focus_dim_ref, "op": "=", "value": focus}]
                if focus else []
            )

            # 维度预选(方向 1):≥2 维且有基期时探测各候选维,取 Σ|Δ| 最大
            # 者作主拆维度(LLM 的顺序不再盲信);探测结果直接复用为 hop1,
            # 不重复查询。share 基线(无基期)信号退化 → 保持计划顺序。
            # focus 存在时探测结果不可复用(focus 会把分组压成单值)。
            d0_ref = dim_refs[0]
            probe_cache: dict[str, Any] = {}
            if (
                config.attribution.probe_dimensions
                and len(dim_refs) >= 2
                and base_period is not None
                and not focus_conds
            ):
                best_sig, best_ref = -1.0, d0_ref
                for ref in dim_refs[: config.attribution.max_dimensions]:
                    cur_map, base_map, sig, hop_c, hop_b = await _probe_dim(
                        connectors, state.datasource, semantic_layer, matched, dialect,
                        metric_name, metric_obj, ratio_parts, ref, [],
                        time_field, cur_period, base_period,
                    )
                    if sig > best_sig:
                        best_sig, best_ref = sig, ref
                        probe_cache = {
                            "cur": cur_map, "base": base_map,
                            "hop_cur": hop_c, "hop_base": hop_b,
                        }
                if best_ref != d0_ref:
                    # 重排:最佳维居首,其余保持计划顺序
                    dim_refs = [best_ref] + [r for r in dim_refs if r != best_ref]
                    d0_ref = dim_refs[0]
            # 记录实际主拆维度(可能被 probe 重排):ref 反查计划维度名
            primary_dim = ref_to_dim.get(d0_ref, dims[0])

            # hop1:按主拆维度分解
            if d0_ref in probe_cache:
                cur_map = probe_cache["cur"]
                base_map = probe_cache["base"]
                if probe_cache.get("hop_cur"):
                    hops.append(probe_cache["hop_cur"])
                if probe_cache.get("hop_base"):
                    hops.append(probe_cache["hop_base"])
            else:
                cur_map, base_map, _sig, hop_c, hop_b = await _probe_dim(
                    connectors, state.datasource, semantic_layer, matched, dialect,
                    metric_name, metric_obj, ratio_parts, d0_ref, focus_conds,
                    time_field, cur_period, base_period,
                )
                if hop_c:
                    hops.append(hop_c)
                if hop_b:
                    hops.append(hop_b)

            effects: dict[str, Any] | None = None
            if is_ratio:
                if base_period is not None and base_map:
                    dec = _shift_share(base_map, cur_map)
                    table = dec["rows"]
                    effects = dec["effects"]
                    base_total = dec["base_total"]
                    cur_total = dec["cur_total"]
                    total_delta = dec["effects"]["delta"]
                else:
                    dec = _ratio_share(cur_map)
                    table = dec["rows"]
                    cur_total = dec["cur_total"]
                    base_total = 0.0
            else:
                table = _contribution(base_map, cur_map)

            # hop2:下钻(depth>=2 且还有第二个维度)——对 top |contribution|
            # 项加过滤后按 dimensions[1] 再分解(比率指标同样 shift-share)。
            drill_table: list[dict[str, Any]] = []
            if depth >= 2 and len(dim_refs) >= 2:
                top = table[0] if table else None
                # 下钻信号用 contribution(比率指标率变化可为 0 但权重移动贡献非 0)
                if top and top["contribution"] != 0:
                    d1_ref = dim_refs[1]
                    drill_conds = [{"field": d0_ref, "op": "=", "value": str(top["dim"])}]
                    if is_ratio:
                        cur_sql = _compile_ratio_hop(
                            semantic_layer, matched, dialect, metric_obj, ratio_parts,
                            d1_ref, drill_conds + _time_conds(time_field, cur_period),
                        )
                        base_sql = (
                            _compile_ratio_hop(
                                semantic_layer, matched, dialect, metric_obj, ratio_parts,
                                d1_ref, drill_conds + _time_conds(time_field, base_period),
                            )
                            if base_period else None
                        )
                        drill_cur: dict[str, Any] = {}
                        drill_base: dict[str, Any] = {}
                        if cur_sql:
                            cols, rows = await _run_hop(connectors, cur_sql, state.datasource)
                            drill_cur = _rows_to_numden(cols, rows)
                            hops.append({"hop": 2, "sql": cur_sql, "columns": cols, "rows": rows[:10], "period": "current", "filter": str(top["dim"])})
                        if base_sql:
                            cols, rows = await _run_hop(connectors, base_sql, state.datasource)
                            drill_base = _rows_to_numden(cols, rows)
                            hops.append({"hop": 2, "sql": base_sql, "columns": cols, "rows": rows[:10], "period": "base", "filter": str(top["dim"])})
                        drill_table = _shift_share(drill_base, drill_cur)["rows"]
                    else:
                        cur_sql = _compile_hop(
                            semantic_layer, matched, dialect, metric_name, [d1_ref],
                            drill_conds + _time_conds(time_field, cur_period),
                        )
                        base_sql = _compile_hop(
                            semantic_layer, matched, dialect, metric_name, [d1_ref],
                            drill_conds + _time_conds(time_field, base_period),
                        )
                        drill_cur_v: dict[str, float] = {}
                        drill_base_v: dict[str, float] = {}
                        if cur_sql:
                            cols, rows = await _run_hop(connectors, cur_sql, state.datasource)
                            drill_cur_v = _rows_to_map(cols, rows)
                            hops.append({"hop": 2, "sql": cur_sql, "columns": cols, "rows": rows[:10], "period": "current", "filter": str(top["dim"])})
                        if base_sql and base_period:
                            cols, rows = await _run_hop(connectors, base_sql, state.datasource)
                            drill_base_v = _rows_to_map(cols, rows)
                            hops.append({"hop": 2, "sql": base_sql, "columns": cols, "rows": rows[:10], "period": "base", "filter": str(top["dim"])})
                        drill_table = _contribution(drill_base_v, drill_cur_v)

            # 归因叙事(LLM,ground 在归因表;走 node_models["attribution"]
            # 覆盖,缺省回落 model_for → model_fast,与 insights 一致)。
            # 比率指标:表格列带率/权重/三效应,并注入分解汇总。
            narrative = ""
            if is_ratio:
                if effects is not None:
                    # shift-share:贡献 = 对整体率的绝对贡献(点),与 delta 同量纲
                    table_text = "\n".join(
                        f"{it['dim']}\t{it['base_rate']:g}\t{it['current_rate']:g}\t"
                        f"{it['base_weight']:.1%}\t{it['current_weight']:.1%}\t"
                        f"{it['within']:g}\t{it['composition']:g}\t{it['interaction']:g}\t"
                        f"{it['contribution']:g}"
                        for it in table[:MAX_ATTRIBUTION_ROWS]
                    )
                else:
                    # share 基线:贡献 = 分子占比(无基期,无率变化可拆)
                    table_text = "\n".join(
                        f"{it['dim']}\t{it['current_rate']:g}\t{it['current_weight']:.1%}\t"
                        f"{it['contribution']:+.1%}"
                        for it in table[:MAX_ATTRIBUTION_ROWS]
                    )
            else:
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
                    prompt_kwargs = dict(
                        question=state.question,
                        metric=metric_name,
                        dimension=primary_dim,
                        baseline=baseline_label,
                        total_delta=total_delta,
                        table=table_text,
                    )
                    if is_ratio:
                        prompt_kwargs["effects"] = effects
                    # ratio + share 基线(无率变化可拆)走普通归因提示词
                    prompt_name = (
                        "attribution/ratio_user"
                        if (is_ratio and effects is not None)
                        else "attribution/user"
                    )
                    response = await llm.chat(
                        model=model,
                        messages=[
                            {"role": "system", "content": render("attribution/system", lang=state.lang)},
                            {"role": "user", "content": render(
                                prompt_name,
                                lang=state.lang,
                                **prompt_kwargs,
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
            if is_ratio and effects is not None:
                chart = _ratio_waterfall_chart(
                    state.question, state.lang, base_total, effects, cur_total,
                )
            else:
                chart = _waterfall_chart(
                    state.question, baseline_label, base_total, cur_total, table, state.lang,
                )

            result = {
                "total_delta": total_delta,
                "table": table,
                "narrative": narrative,
                "hops": hops,
                "dimensions": [primary_dim] + [d for d in dims if d != primary_dim],
                "baseline": baseline,
                "chart": chart,
                "metric": metric_name,
                "kind": "ratio" if is_ratio else "additive",
            }
            if effects is not None:
                result["effects"] = effects
            # 下钻表并入(有则挂到 result,供前端分析面板/后续洞察复用)
            if drill_table:
                drill_dim = (
                    ref_to_dim.get(dim_refs[1], dims[1])
                    if len(dim_refs) >= 2 else primary_dim
                )
                result["drilldown"] = {
                    "dimension": drill_dim,
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
