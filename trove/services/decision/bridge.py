"""分析→决策桥 —— 判定触发后,附带"为什么"的确定性证据(补丁 1)。

雪花的 alert / Databricks 的告警只给「阈值 + 数字」,没人回答「为什么」。
Trove 的判定触发时,这里把 P1 的分析引擎接上去,产出一份**摘要**(不是
叙事,零 LLM):

    {"top_components": [{dim, value, contribution}],   # 谁推动了变化
     "residual": {value, exact, reason},                # 分解的诚实残差
     "tree": {...},                                     # 驱动器树证据
     "queries": [...],                                  # 可复算的 SQL
     "degraded": [...]}                                 # 哪一步没做成

三条纪律:

**只在触发时跑**(``evaluate_rule`` 触发分支调用),未触发零成本 —— 判定
是常态,分析是例外,不能让每条不触发的规则都背两次查询。

**预算硬上限**:整座桥 ≤ ``MAX_BRIDGE_QUERIES``(4)条查询,超了就记账
``degraded: query_budget_exceeded`` 并交出已经拿到的部分 —— 超预算不是
错误,是事实,记下来即可(响亮不阻断)。

**不适用 ≠ 失败**:指标表达式不可分解(单叶子)、时间字段无法解析 —— 这些
是"桥对这条规则没有话说",返回 ``None``,证据里什么也不写;只有**该做而
没做成**(编译失败、SQL 报错、超时)才写 ``degraded``。两者的区别就是
"规则不适合"与"桥坏了"的区别。
"""

from __future__ import annotations

from typing import Any

from trove.core.logging import get_logger
from trove.services.analysis.engine import (
    AnalysisEngine,
    AnalysisLimits,
    AnalysisRequest,
    compile_hop,
    resolve_dim_ref,
    resolve_metric,
    resolve_time_field,
    rows_to_map,
    time_conds,
)
from trove.services.decision.expr import as_number
from trove.services.decision.rules import DecisionRule

logger = get_logger(__name__)

#: 整座桥的查询预算(补丁 1 拍板):hop0 2 + 树 2,或维度分组 2 + 树 2。
MAX_BRIDGE_QUERIES = 4
#: 树收集的组件数上限(拍板值)。
MAX_BRIDGE_COMPONENTS = 3
#: ``top_components`` 保留的条数 —— 一行通知里超过三条就没人读了。
TOP_COMPONENTS = 3


def _contribution(items: list[dict[str, Any]]) -> None:
    """就地补 ``contribution`` = Δ / Σ|Δ|(与判定行同一口径)。

    用 Σ|Δ| 而不是 Δ_root 作分母:分子分母都可能有相反符号(比率链的
    分子分母常一升一降),除以净差会放大成 300%、-800% 这种读不懂的数;
    Σ|Δ| 回答的是"这一份占全部波动量的几成",这正是「主因」要表达的。
    分母为 0 → None(不是 0:0 是"没有波动",None 是"算不出占比")。
    """
    total_abs = 0.0
    for it in items:
        d = as_number(it.get("delta"))
        if d is not None:
            total_abs += abs(d)
    for it in items:
        d = as_number(it.get("delta"))
        it["contribution"] = (d / total_abs) if (d is not None and total_abs) else None


def _tree_top_components(tree: dict[str, Any]) -> list[dict[str, Any]]:
    """树根的下一层组件 → ``top_components``(按 |Δ| 降序)。"""
    out: list[dict[str, Any]] = []
    for ch in (tree or {}).get("children") or []:
        if not isinstance(ch, dict):
            continue
        d = as_number(ch.get("delta"))
        if d is None:
            continue
        out.append({
            "dim": str(ch.get("name") or ch.get("metric") or ch.get("candidate") or ""),
            "value": d,
            "delta": d,
            "source": "tree",
        })
    out.sort(key=lambda c: abs(c["value"]), reverse=True)
    return out[:TOP_COMPONENTS]


def _rows_top_components(
    dimension: str, rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """判定行(per_dimension)→ ``top_components``(零额外查询的复用路径)。

    标签直接来自判定行 —— 同一次判定用的同一批数字,桥与 verdict 不可能
    对不上。
    """
    out: list[dict[str, Any]] = []
    for r in rows or []:
        d = as_number(r.get("delta"))
        if d is None:
            continue
        out.append({
            "dim": dimension,
            "value": str(r.get("dim") or ""),
            "delta": d,
            "contribution": as_number(r.get("contribution")),
            "source": "dimension",
        })
    out.sort(key=lambda c: abs(c["delta"]), reverse=True)
    return out[:TOP_COMPONENTS]


def _fmt_num(value: Any) -> str:
    n = as_number(value)
    if n is None:
        return "—"
    return f"{n:,.2f}".rstrip("0").rstrip(".")


def primary_driver_line(summary: dict[str, Any] | None) -> str:
    """``top_components`` → 「主因：region=华东（贡献 62%）」(纯函数)。

    单条择首 —— 一行通知只能有一个主因,其余在证据里。没有任何可说的
    (桥不适用、组件全无数值)→ 空串,调用方据此不追加。
    """
    comps = (summary or {}).get("top_components") or []
    if not comps:
        return ""
    top = comps[0]
    label = f"{top.get('dim')}={top.get('value')}" if top.get("source") == "dimension" \
        else f"{top.get('dim')}={_fmt_num(top.get('value'))}"
    pct = as_number(top.get("contribution"))
    if pct is None:
        return label
    return f"{label}（贡献 {pct * 100:.1f}%）"


async def run_bridge(
    *,
    semantic_layer: Any,
    runner: Any,
    rule: DecisionRule,
    datasource: str,
    dialect: str,
    cur_period: tuple[str, str] | None,
    base_period: tuple[str, str] | None,
    judged_rows: list[dict[str, Any]] | None = None,
    matched: list[str] | None = None,
) -> dict[str, Any] | None:
    """触发后的分析摘要;``None`` = 桥对这条规则不适用。

    ``matched`` = 判定路径编译时用的锚定数据集(service 把它从
    ``build_and_compile`` 的产物里原样递进来)。缺省时回落到度量声明锚定
    —— 但**派生度量的 datasets 会是空的**(oSSIE 的 ``_dataset_refs`` 只认
    ``dataset.field``,不认被引用的度量名),桥必须优先用判定那一份。

    绝不上抛:桥的任何失败都折进 ``degraded`` —— 判定已经判完了,桥只是
    附录。
    """
    try:
        return await _run(
            semantic_layer=semantic_layer, runner=runner, rule=rule,
            datasource=datasource, dialect=dialect, cur_period=cur_period,
            base_period=base_period, judged_rows=judged_rows or [],
            matched=list(matched or []),
        )
    except Exception as e:  # pragma: no cover - 防御:桥绝不能拖垮判定
        logger.exception("analysis bridge crashed for rule %s", rule.id)
        return {
            "top_components": [], "tree": None,
            "residual": {"value": None, "exact": False, "reason": "bridge_error"},
            "queries": [],
            "degraded": [{"stage": "analysis_bridge", "reason": str(e)[:200]}],
        }


def _driver_dimension(rule: DecisionRule) -> str:
    """桥沿哪个维度找主因(缺省 = 规则自己的分组维度)。

    显式声明优先(``driver_dimension``,lint 校验其已在语义模型声明);
    缺省用规则的 subject 维度 —— 单维 per_dimension 规则的判定行本身就是
    该维的分组值,零额外查询。多维拼接标签("a / b")无法拆回单维,按
    "没有可用维度"处理:宁可不给,不给错的。
    """
    if rule.driver_dimension:
        return rule.driver_dimension
    if rule.scope == "per_dimension" and len(rule.subject.dimensions) == 1:
        return rule.subject.dimensions[0]
    return ""


async def _dimension_components(
    *, semantic_layer: Any, runner: Any, rule: DecisionRule, datasource: str,
    dialect: str, matched: list[str], dimension: str, time_field: str,
    cur_period: Any, base_period: Any, degraded: list[dict[str, Any]],
    queries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """跨维度分组的两期取值 → ``top_components``(2 条查询)。

    走引擎的 ``compile_hop`` —— 与树同一套编译纪律(GROUP BY/解析失败
    即记账,不硬凑)。这两条 SQL **跑在引擎之外**,所以要自己进证据
    (``queries``,与引擎同一形状):判定的可复算性不认"谁跑的",只认
    "SQL 在不在证据里"。
    """
    metric_name = rule.subject.metrics[0]
    dim_ref = resolve_dim_ref(semantic_layer, matched, dimension)
    if dim_ref is None:
        degraded.append({"stage": "analysis_bridge",
                         "reason": f"driver_dimension_unresolved:{dimension}"[:120]})
        return []
    maps: dict[str, dict[str, float]] = {}
    for tag, period in (("current", cur_period), ("base", base_period)):
        if period is None:
            continue
        sql = ""
        try:
            sql = compile_hop(semantic_layer, matched, dialect, metric_name,
                              [dim_ref], time_conds(time_field, period, dialect=dialect))
            if not sql:
                continue
            cols, rows = await runner(sql, datasource)
            maps[tag] = rows_to_map(list(cols), list(rows))
            queries.append({
                "id": len(queries) + 1,
                "purpose": "driver_dimension",
                "sql": sql,
                "columns": [str(c) for c in cols],
                "row_count": len(rows),
                "rows": [list(r) for r in rows[:10]],
                "truncated": len(rows) > 10,
                "period": tag,
                "dimension": dimension,
            })
        except Exception as e:
            entry: dict[str, Any] = {
                "stage": "analysis_bridge",
                "reason": f"{tag}_group_failed:{str(e)[:120]}",
            }
            if sql:
                entry["sql"] = sql[:400]
            degraded.append(entry)
    cur_map, base_map = maps.get("current", {}), maps.get("base", {})
    items: list[dict[str, Any]] = []
    for label in list(cur_map) + [k for k in base_map if k not in cur_map]:
        c, b = cur_map.get(label), base_map.get(label)
        if c is None or b is None:
            continue
        items.append({"dim": dimension, "value": str(label),
                      "delta": c - b, "source": "dimension"})
    _contribution(items)
    items.sort(key=lambda c: abs(c["delta"]), reverse=True)
    return items[:TOP_COMPONENTS]


async def _run(
    *, semantic_layer: Any, runner: Any, rule: DecisionRule, datasource: str,
    dialect: str, cur_period: Any, base_period: Any,
    judged_rows: list[dict[str, Any]], matched: list[str],
) -> dict[str, Any] | None:
    metric_name = rule.subject.metrics[0] if rule.subject.metrics else ""
    if not metric_name:
        return None

    # 匹配集必须与判定路径**同源**:空 matched 会让时间字段/维度解析一律
    # 判否(编译器"不猜"),桥静默退化成"永远不适用" —— 一条永远不响的
    # 附录,与"这条规则没什么可分析的"无从区分。判定方递来的锚定优先;
    # 没有时回落到度量声明锚定(注意派生度量的 datasets 常为空,见
    # ossie._dataset_refs:它只认 dataset.field,不认被引用的度量名)。
    if not matched:
        metric_obj = resolve_metric(semantic_layer, metric_name)
        matched = [str(d) for d in
                   (getattr(metric_obj, "datasets", None) or []) if d]

    dim = _driver_dimension(rule)
    reused = bool(dim) and rule.scope == "per_dimension" \
        and dim in rule.subject.dimensions and len(rule.subject.dimensions) == 1
    dim_query = bool(dim) and not reused
    # 预算切法:维度分组要花的 2 条从引擎额度里先扣,树才不会把预算吃光。
    engine_budget = MAX_BRIDGE_QUERIES - (2 if dim_query else 0)

    # 引擎的根总量(hop0)只在"补上它也不挤占树"时跑 —— 见 run_components。
    include_total = not dim_query
    engine = AnalysisEngine(
        semantic_layer, runner,
        AnalysisLimits(
            max_dimensions=1, max_hops=0, probe_dimensions=False,
            ratio_decomposition=True, driver_tree=True,
            max_components=MAX_BRIDGE_COMPONENTS, max_queries=engine_budget,
        ),
    )
    request = AnalysisRequest(
        question=rule.describe(), lang="zh",
        datasource=datasource, dialect=dialect, matched=matched,
        metric=metric_name, dimensions=[dim] if dim else [],
        baseline=rule.baseline.kind, periods=(cur_period, base_period),
    )
    outcome = await engine.run_components(request, include_total=include_total)
    if outcome is None:
        return None      # 不适用:表达式不可分解 / 无时间字段 / 无当前期

    degraded: list[dict[str, Any]] = list(outcome.degraded)
    queries: list[dict[str, Any]] = list(outcome.evidence_queries)
    tree = outcome.tree or {}
    if dim and reused:
        comps = _rows_top_components(dim, judged_rows)
    elif dim_query:
        time_field = resolve_time_field(semantic_layer, matched, metric_name) or ""
        if time_field:
            comps = await _dimension_components(
                semantic_layer=semantic_layer, runner=runner, rule=rule,
                datasource=datasource, dialect=dialect, matched=matched,
                dimension=dim, time_field=time_field, cur_period=cur_period,
                base_period=base_period, degraded=degraded, queries=queries,
            )
        else:
            comps = []
            degraded.append({"stage": "analysis_bridge",
                             "reason": "no_time_field_for_driver_dimension"})
    else:
        comps = _tree_top_components(tree)

    residual = tree.get("residual") or {
        "value": None, "exact": False, "reason": "not_computed"}
    return {
        "top_components": comps,
        "tree": tree or None,
        "residual": residual,
        "queries": queries,
        "degraded": degraded,
    }
