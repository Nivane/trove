"""分析引擎 —— 编排:请求 → 取数 → 分解 → payload。

**全服务唯一的 I/O 处**:所有查询经注入的 ``runner`` 执行
(``async (sql, datasource) -> (columns, rows)``),引擎自己不开连接、
不 import 数据源注册表。分解数学在 ``decompose``、树骨架在
``expr_tree``、渲染在 ``render`` —— 本模块只做「先查什么、再查什么」
与记账(queries / degraded / partial)。

编排流程与 ``workflow/nodes/attribution.py`` 原节点逐行同构
(hop0 总量 → 维度预选 → hop1 分解 → hop2 下钻),行为不变量由
``tests/workflow/test_attribution.py`` 零改动通过钉死;追加的
驱动器树是**新阶段**,只受新配置门控,失败只降级不影响老路径。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from trove.core.logging import get_logger
from trove.core.periods import base_period as _derive_periods
from trove.core.serialize import json_safe
from trove.services.analysis.decompose import (
    breakdown_signal,
    conservation_gap,
    conservation_violated,
    contribution,
    num,
    ratio_share,
    residual,
    shift_share,
    signed_children,
)
from trove.services.analysis.budget import QueryLedger
from trove.services.analysis.expr_tree import (
    collect_components,
    metric_components,
    metric_ratio_parts,
)
from trove.services.analysis.series import (
    SeriesSpec,
    block_windows,
    compile_series_hop,
    derive_grain,
    same_phase_blocks,
    series_from_rows,
)
from trove.services.analysis.stats import (
    band,
    confidence_from_margin,
    low_n,
    outside,
    robust_z,
)

logger = get_logger(__name__)

#: 注入的执行器:只读一跳 SQL → (columns, rows)。超时/失败抛错由 runner 负责。
HopRunner = Callable[[str, str], Awaitable[tuple[list[str], list[list[Any]]]]]


def _record_rows(rows: list[list[Any]], keep: int) -> list[list[Any]]:
    """证据/跳记录的截断视图:截到 ``keep`` + 逐格 JSON 安全化。

    数学路径不经过这里 —— ``rows_to_map`` / ``rows_to_numden`` / ``num``
    继续吃 ``_execute`` 交回的原始行(它们自己 float 化)。记录视图是要进
    payload → summary → 落库/SSE 的旁路,``Decimal`` / ``date`` 原样进去
    会让 ``json.dumps`` 在**交付段**抛错:2026-10-05 归因答案在 UI 上
    「流中断」的根因(见 ``core.serialize``)。
    """
    return [[json_safe(cell) for cell in row] for row in rows[:keep]]


# ── 语义模型解析(纯,只读模型)────────────────────────────

def resolve_time_field(semantic_layer: Any, matched: list[str], metric_name: str) -> str | None:
    """度量锚定的声明时间字段引用(dataset.field);不可判定 → None。"""
    try:
        from trove.services.semantic_layer.compiler import SemanticCompiler, resolve_time_field as _rtf

        model = semantic_layer.model()
        if model is None:
            return None
        compiler = SemanticCompiler(model)
        metric = compiler._metric_by_name(metric_name)
        preferred = metric.agg_time_dimension if metric is not None else ""
        resolved = _rtf(model, list(matched), preferred=preferred)
        if resolved is None:
            return None
        return f"{resolved[0]}.{resolved[1].name}"
    except Exception:
        return None


def resolve_dim_ref(semantic_layer: Any, matched: list[str], dim: str) -> str | None:
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


def resolve_metric(semantic_layer: Any, metric_name: str) -> Any:
    """语义模型里的目标度量对象;解析失败 → None。"""
    try:
        from trove.services.semantic_layer.compiler import SemanticCompiler

        model = semantic_layer.model()
        if model is None:
            return None
        return SemanticCompiler(model)._metric_by_name(metric_name)
    except Exception:
        return None


# ── SQL 构造(复用语义编译器:hops 由编译器直接产出,失败即降级)────────

def compile_hop(
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


def compile_ratio_hop(
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


def _plus_one_day(iso: str) -> str | None:
    """ISO 日期 + 1 天(不可解析 → None,调用方回退)。"""
    try:
        return (date.fromisoformat(str(iso)) + timedelta(days=1)).isoformat()
    except (TypeError, ValueError):
        return None


def time_conds(
    time_field: str,
    period: tuple[str, str] | None,
    *,
    dialect: str = "",
) -> list[dict[str, Any]]:
    """时间范围 → plan conditions(半开 ``>= start`` ∧ ``< end + 1 天``)。

    半开是唯一对 Date 与 Timestamp 列语义一致的形式:闭区间
    ``<= '2024-01-31'`` 在 timestamp 列上排除当天 00:00 之后的所有行
    —— **丢最后一天**(sqlite 实测:同日数据 date 列 2 行、timestamp
    列 0 行;半开则两列同为 2 行)。end 不可解析时回退闭区间(如实
    标注,不猜)。``dialect`` 预留给未来需要方言差异化时间算术的场景,
    当前四种方言统一按 ISO 字面量比较。
    """
    if not time_field or period is None:
        return []
    start, end = period
    tail = _plus_one_day(end)
    if tail is None:
        return [
            {"field": time_field, "op": ">=", "value": start, "note": "attribution period start"},
            {"field": time_field, "op": "<=", "value": end, "note": "attribution period end (unparsable; closed fallback)"},
        ]
    return [
        {"field": time_field, "op": ">=", "value": start, "note": "attribution period start"},
        {"field": time_field, "op": "<", "value": tail, "note": "attribution period end (half-open: end + 1d)"},
    ]


def rows_to_map(columns: list[str], rows: list[list[Any]]) -> dict[str, float]:
    """hop 结果(维度, 度量)→ {dim: value}。首列为维度,末列为度量。"""
    out: dict[str, float] = {}
    if not columns or not rows:
        return out
    for row in rows:
        if len(row) < 2:
            continue
        out[str(row[0])] = num(row[-1])
    return out


def rows_to_numden(columns: list[str], rows: list[list[Any]]) -> dict[str, tuple[float, float]]:
    """比率 hop 结果(维度, 分子, 分母)→ {dim: (num, den)}。"""
    out: dict[str, tuple[float, float]] = {}
    if not columns or not rows:
        return out
    for row in rows:
        if len(row) < 3:
            continue
        out[str(row[0])] = (num(row[1]), num(row[2]))
    return out


# ── 请求 / 限度 / 结果 ────────────────────────────────────

@dataclass
class AnalysisLimits:
    """确定性预算(节点从 AgentConfig 装配;默认值 = 存量行为)。"""

    max_dimensions: int = 3
    max_hops: int = 2
    probe_dimensions: bool = True
    ratio_decomposition: bool = True
    driver_tree: bool = True
    max_components: int = 4
    max_queries: int = 12
    tree_max_depth: int = 5
    #: 块序列(统计器械,B1)。``series_grain`` 空 = 新阶段关闭(老路径
    #: 逐字节不变);非空时作为默认粒度(请求的 ``SeriesSpec.grain`` 优先)。
    series_grain: str = ""
    block_lookback: int = 12
    #: 全运行查询硬预算(None = 不设上限,只记账 —— 老路径逐字节兼容)。
    #: 「老路径不受 max_queries 约束」的洞由此从**不可见变可观测**;
    #: 上限只在显式设置时生效(决策桥/新阶段),让路永远记账、永不静默。
    total_query_budget: int | None = None


@dataclass
class AnalysisRequest:
    """一次分析请求(节点/API 构造;引擎不读 state)。"""

    question: str
    lang: str
    datasource: str
    dialect: str
    matched: list[str]
    metric: str
    dimensions: list[str]
    baseline: str = "prev_period"
    depth: int = 1
    focus: str | None = None
    time_context: str | None = None
    #: 已解析的显式期间 (cur, base) ISO 覆盖 —— 决策桥用:规则窗口已由
    #: ``DecisionService._resolve_window`` 按**运行锚点**解析过,二次解析
    #: (且按"今天"解析)会给出不同窗口,判定与证据就对不上了。
    #: None = 走 ``time_context`` 的常规派生(问答路径不变)。
    periods: tuple[tuple[str, str] | None, tuple[str, str] | None] | None = None
    #: 块序列规格(B1 统计器械;None = 不跑序列阶段 —— 老路径不变)。
    series: SeriesSpec | None = None


@dataclass
class AnalysisOutcome:
    """分解产物(节点据此组装 state.attribution 与 state.analysis)。"""

    metric: str
    baseline: str
    table: list[dict[str, Any]] = field(default_factory=list)
    effects: dict[str, Any] | None = None
    total_delta: float = 0.0
    base_total: float = 0.0
    cur_total: float = 0.0
    primary_dim: str = ""
    dimensions: list[str] = field(default_factory=list)
    drilldown: dict[str, Any] | None = None
    is_ratio: bool = False
    hops: list[dict[str, Any]] = field(default_factory=list)
    tree: dict[str, Any] | None = None
    evidence_queries: list[dict[str, Any]] = field(default_factory=list)
    degraded: list[dict[str, Any]] = field(default_factory=list)
    partial: bool = False
    #: 异常中途降级(表为空、只有 hops)—— 节点按旧形状原样组装 result
    degraded_result: bool = False
    #: 块序列产物(统计器械;None = 未跑/降级 —— payload 不出本节)。
    series: dict[str, Any] | None = None
    #: 查询账本快照(仅 ``total_query_budget`` 显式设置时非 None ——
    #: 老路径 evidence 不带 budget 键,逐字节兼容)。
    budget: dict[str, Any] | None = None


class AnalysisEngine:
    """一次分析运行的编排器(实例即一次运行:hops/evidence 行程内累积)。"""

    def __init__(
        self,
        semantic_layer: Any,
        runner: HopRunner,
        limits: AnalysisLimits | None = None,
    ) -> None:
        self._sl = semantic_layer
        self._runner = runner
        self.limits = limits or AnalysisLimits()
        self._ledger = QueryLedger(total=self.limits.total_query_budget)
        self._hops: list[dict[str, Any]] = []
        self._evidence: list[dict[str, Any]] = []
        self._queries = 0
        self._produced = False  # 主分解表是否产出过(降级分支判定用)
        self._hop0_ok = False  # hop0 当前期总量是否真取到(树根口径覆盖的前置)

    # ── 记账 ────────────────────────────────────────────

    async def _execute(
        self,
        sql: str,
        datasource: str,
        *,
        purpose: str,
        period: str = "",
        filt: str = "",
        keep: int = 10,
    ) -> tuple[list[str], list[list[Any]]]:
        """唯一执行入口:跑一跳 + 记证据(query 计数/rows 截断视图)。"""
        cols, rows = await self._runner(sql, datasource)
        self._queries += 1
        self._ledger.record(purpose)
        entry: dict[str, Any] = {
            "id": len(self._evidence) + 1,
            "purpose": purpose,
            "sql": sql,
            "columns": list(cols),
            "row_count": len(rows),
            "rows": _record_rows(rows, keep),
            "truncated": len(rows) > keep,
        }
        if period:
            entry["period"] = period
        if filt:
            entry["filter"] = filt
        self._evidence.append(entry)
        return list(cols), list(rows)

    async def _probe_dim(
        self,
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
        datasource: str,
    ) -> tuple[dict[str, Any], dict[str, Any], float, dict[str, Any] | None, dict[str, Any] | None]:
        """一个候选维的双期 GROUP BY 探测 → (cur_map, base_map, signal, hop_cur, hop_base)。

        hop_cur/hop_base: 成功执行时该跳的观测条目(记录用),失败 → None。
        比率度量用 num/den 双列;加性用单值列。
        """
        hop_cur = hop_base = None
        if ratio_parts is not None and metric is not None:
            cur_sql = compile_ratio_hop(
                self._sl, matched, dialect, metric, ratio_parts,
                dim_ref, conds_extra + time_conds(time_field, cur_period, dialect=dialect),
            )
            base_sql = (
                compile_ratio_hop(
                    self._sl, matched, dialect, metric, ratio_parts,
                    dim_ref, conds_extra + time_conds(time_field, base_period, dialect=dialect),
                )
                if base_period else None
            )
            cur_map: dict[str, Any] = {}
            base_map: dict[str, Any] = {}
            if cur_sql:
                cols, rows = await self._execute(cur_sql, datasource, purpose="probe", period="current")
                cur_map = rows_to_numden(cols, rows)
                hop_cur = {"hop": 1, "sql": cur_sql, "columns": cols, "rows": _record_rows(rows, 10), "period": "current"}
            if base_sql:
                cols, rows = await self._execute(base_sql, datasource, purpose="probe", period="base")
                base_map = rows_to_numden(cols, rows)
                hop_base = {"hop": 1, "sql": base_sql, "columns": cols, "rows": _record_rows(rows, 10), "period": "base"}
        else:
            cur_sql = compile_hop(
                self._sl, matched, dialect, metric_name, [dim_ref],
                conds_extra + time_conds(time_field, cur_period, dialect=dialect),
            )
            base_sql = (
                compile_hop(
                    self._sl, matched, dialect, metric_name, [dim_ref],
                    conds_extra + time_conds(time_field, base_period, dialect=dialect),
                )
                if base_period else None
            )
            cur_map = {}
            base_map = {}
            if cur_sql:
                cols, rows = await self._execute(cur_sql, datasource, purpose="probe", period="current")
                cur_map = rows_to_map(cols, rows)
                hop_cur = {"hop": 1, "sql": cur_sql, "columns": cols, "rows": _record_rows(rows, 10), "period": "current"}
            if base_sql:
                cols, rows = await self._execute(base_sql, datasource, purpose="probe", period="base")
                base_map = rows_to_map(cols, rows)
                hop_base = {"hop": 1, "sql": base_sql, "columns": cols, "rows": _record_rows(rows, 10), "period": "base"}
        signal = breakdown_signal(cur_map, base_map, ratio_parts)
        return cur_map, base_map, signal, hop_cur, hop_base

    # ── 块序列(统计器械 B1;默认关,失败只降级)─────────────

    def _series_spec(self, request: AnalysisRequest) -> SeriesSpec | None:
        """块规格:请求 > 限额默认;两者皆无 → None(新阶段关闭)。"""
        if request.series is not None:
            return request.series
        if self.limits.series_grain:
            return SeriesSpec(
                grain=self.limits.series_grain,
                lookback=self.limits.block_lookback,
            )
        return None

    async def _series_stage(
        self,
        request: AnalysisRequest,
        metric_name: str,
        time_field: str | None,
        cur_period: tuple[str, str] | None,
        datasource: str,
        degraded: list[dict[str, Any]],
        *,
        current: float | None = None,
    ) -> dict[str, Any] | None:
        """历史块分布(1 条 ``time_grain`` 查询)→ band / z / outside。

        块严格取**当前窗口之前**(噪声带混入被测点会自我稀释:异常值
        把自己拉回带内);粒度与窗口不对齐 / 时间字段缺失 / 编译失败 /
        预算让路 → 记 ``degraded`` 后返回 None —— 序列是**增强**不是
        前提,主产物照常交付(判定侧的 ``require:outside_band`` 会把
        「算不出」升级为 error run,那是判定语义,不是引擎语义)。
        """
        spec = self._series_spec(request)
        if spec is None:
            return None
        if not time_field or cur_period is None:
            degraded.append({"stage": "series", "reason": "no_time_field"})
            return None
        grain = str(spec.grain or "").strip() or derive_grain(cur_period)
        if grain is None:
            degraded.append({"stage": "series", "reason": "grain_unaligned"})
            return None
        mode = str(spec.mode or "trailing").strip().lower()
        count = max(int(spec.lookback or 0), 0)
        if mode == "same_phase":
            blocks = same_phase_blocks(cur_period, count)
        else:
            mode = "trailing"
            blocks = block_windows(cur_period, grain, count)
        if not blocks:
            degraded.append({"stage": "series", "reason": "no_blocks"})
            return None
        # 预算门(显式,且只在新阶段):无上限时 can() 恒真
        if not self._ledger.can(1):
            entry = self._ledger.yield_("series", needed=1)
            degraded.append({"stage": "series", "reason": entry["reason"]})
            return None
        span = (blocks[0][0], blocks[-1][1])
        dialect = request.dialect or "sqlite"
        sql = compile_series_hop(
            self._sl, list(request.matched), dialect, metric_name,
            time_conds(time_field, span, dialect=dialect),
            time_grain=grain, time_field=time_field,
        )
        if sql is None:
            degraded.append({"stage": "series", "reason": "compile_miss"})
            return None
        try:
            cols, rows = await self._execute(sql, datasource, purpose="series")
        except Exception as e:
            logger.warning("Series stage execution failed (%s); skipping", e)
            degraded.append({"stage": "series", "reason": str(e)[:200]})
            return None
        series = series_from_rows(cols, rows)
        if mode == "same_phase":
            # 单条范围查询会带回异相位块 —— 只留落在目标块内的桶。
            # 比较键按桶标签长度截齐:sqlite 月桶标签是 'YYYY-MM'(7 位),
            # 直接与 'YYYY-MM-DD' 窗口边界比较会因前缀短而全部漏掉。
            series = [
                (lbl, v) for lbl, v in series
                if any(w0[:len(lbl)] <= lbl <= w1[:len(lbl)] for w0, w1 in blocks)
            ]
        if not series:
            degraded.append({"stage": "series", "reason": "empty_series"})
            return None
        values = [v for _, v in series]
        k = float(spec.k or 3.5)
        b = band(values, k=k)
        z = robust_z(current, values) if current is not None else None
        return {
            "grain": grain,
            "mode": mode,
            "lookback": count,
            "span": [span[0], span[1]],
            "labels": [lbl for lbl, _ in series],
            "values": values,
            "band": b.to_dict(),
            "current": current,
            "z": z,
            "outside": outside(current, b) if current is not None else None,
            "low_n": low_n(len(values)),
            # 带宽与位置分数(B8)随序列走:消费面(答案 markdown / 分析卡)
            # **不重算** —— 与判定侧 gate 同一份公式
            # (``confidence_from_margin``),「位置分数非概率」的限定语
            # 留在各自的出口措辞里。
            "k": k,
            "confidence": confidence_from_margin(z, k),
        }

    def _budget_snapshot(self) -> dict[str, Any] | None:
        """账本快照(仅显式预算时非 None —— 老路径 payload 无 budget 键)。"""
        if self.limits.total_query_budget is None:
            return None
        return self._ledger.snapshot()

    # ── 驱动器树(新阶段;失败只降级,不动老路径)──────────────

    async def _build_tree(
        self,
        request: AnalysisRequest,
        metric_obj: Any,
        cur_period: tuple[str, str] | None,
        base_period: tuple[str, str] | None,
        time_field: str | None,
        cur_total: float,
        base_total: float,
        degraded: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """指标表达式树 → 组件值(两期各一条多度量 SQL)。失败 → 骨架或 None。"""
        lim = self.limits
        if metric_obj is None:
            return None
        try:
            model = self._sl.model()
        except Exception:
            model = None
        if model is None:
            return None

        root = metric_components(metric_obj, model, max_depth=lim.tree_max_depth)
        comps, truncated = collect_components(
            root, max_components=lim.max_components, exclude=request.metric,
        )
        if truncated:
            degraded.append({"stage": "driver_tree", "reason": f"components_truncated:{truncated}"})

        # 预检(**只编译不执行,零查询成本**):逐候选独立编译,把
        # ①不可解析 ②名字被字段遮蔽(编译器"字段优先"会把裸名当维度投影,
        # 值变成分组列) ③结果带 GROUP BY(维度混入) 的候选剔除。
        # 按"解析后的度量名"去重 —— 编译器投影按度量名去重,同名会少列,
        # 位置映射会错位;别名表让不同拼写(如 SUM(x) 与度量名)共用一值。
        from trove.services.semantic_layer.compiler import SemanticCompiler
        from trove.services.semantic_layer.query import (
            SemanticQuery,
            _resolve_metric as _sq_resolve,
            build_and_compile,
        )

        def _filters(period: tuple[str, str] | None) -> list[dict[str, Any]]:
            return [
                {"field": c["field"], "op": c["op"], "value": c["value"]}
                for c in time_conds(time_field, period, dialect=request.dialect or "sqlite")
            ]

        def _compile_one(cand: str, period: tuple[str, str] | None) -> str | None:
            """单候选编译;解析失败/维度投影(GROUP BY)→ None。"""
            try:
                compiled = build_and_compile(
                    model, SemanticQuery(metrics=[cand], filters=_filters(period)),
                    dialect=request.dialect or "sqlite", matched=list(request.matched),
                )
            except Exception:
                return None
            if "GROUP BY" in compiled["sql"].upper():
                return None
            return compiled["sql"]

        compiler = SemanticCompiler(model)
        kept: list[dict[str, Any]] = []  # {"candidate","resolved","sql_cur","sql_base"}
        alias: dict[str, str] = {}      # 组件候选 → 解析后度量名(值映射键)
        seen_resolved: set[str] = set()
        for cand in [request.metric] + [c["candidate"] for c in comps]:
            try:
                resolved = _sq_resolve(model, cand)
            except Exception:
                degraded.append({"stage": "driver_tree", "reason": f"unresolvable:{cand}"[:120]})
                continue
            # 裸名被字段遮蔽 → 编译器会走字段路径(维度投影),不可用于取值
            if "(" not in str(cand):
                try:
                    shadowed = compiler._resolve_field(str(cand).strip(), set(request.matched)) is not None
                except Exception:
                    shadowed = False
                if shadowed:
                    degraded.append({"stage": "driver_tree", "reason": f"field_shadowed:{cand}"[:120]})
                    continue
            key = str(getattr(resolved, "name", "") or cand)
            if key in seen_resolved:
                alias[str(cand)] = key  # 同名度量:共享首见候选的值
                continue
            sql_cur = _compile_one(cand, cur_period)
            sql_base = _compile_one(cand, base_period) if base_period else None
            if sql_cur is None and sql_base is None:
                degraded.append({"stage": "driver_tree", "reason": f"not_aggregate:{cand}"[:120]})
                continue
            seen_resolved.add(key)
            alias[str(cand)] = key
            kept.append({"candidate": cand, "resolved": key,
                         "sql_cur": sql_cur, "sql_base": sql_base})

        def _finish() -> dict[str, Any]:
            """根节点数值优先用 hop0 官方口径(与卡片头条一致);hop0 没跑成
            (编译失败)时保留树查询自己的值,不拿 0.0 顶替。所有返回路径
            (含预算截断/空候选的骨架)都要过这里 —— 整体值在，头就不断。"""
            if self._hop0_ok:
                root["current"] = cur_total
                root["base"] = base_total
                root["delta"] = cur_total - base_total
                root["executed"] = True
                root["value_source"] = "hop0"
            elif root.get("executed"):
                root["value_source"] = "tree_query"
            else:
                root["value_source"] = "none"
            return root

        need = 2 if (cur_period and base_period) else 1
        if self._queries + need > lim.max_queries:
            degraded.append({"stage": "driver_tree", "reason": "query_budget_exceeded"})
            self._mark_tree(root, comps, {}, {}, alias=alias,
                            cur_total=cur_total, base_total=base_total)
            return _finish()
        if not kept:
            self._mark_tree(root, comps, {}, {}, alias=alias,
                            cur_total=cur_total, base_total=base_total)
            return _finish()

        cur_vals: dict[str, float] = {}
        base_vals: dict[str, float] = {}

        def _fill(bucket: dict[str, float], keys: list[str], row: list[Any]) -> None:
            for i, key in enumerate(keys):
                bucket[key] = num(row[i]) if i < len(row) else 0.0

        try:
            combined_ok = True
            combined: dict[str, dict[str, Any]] = {}
            for period_tag, period in (("current", cur_period), ("base", base_period)):
                if period is None:
                    continue
                try:
                    compiled = build_and_compile(
                        model,
                        SemanticQuery(metrics=[k["candidate"] for k in kept], filters=_filters(period)),
                        dialect=request.dialect or "sqlite", matched=list(request.matched),
                    )
                except Exception:
                    combined_ok = False
                    break
                combined[period_tag] = compiled
            if combined_ok and any("GROUP BY" in c["sql"].upper() for c in combined.values()):
                combined_ok = False  # 组合路径混入维度投影 → 逐候选回退

            if combined_ok:
                # 组合路径:每期一条多度量 SQL;列序 == kept 顺序(已按
                # 解析名去重,列数与候选数一致)
                for period_tag, bucket in (("current", cur_vals), ("base", base_vals)):
                    if period_tag not in combined:
                        continue
                    cols, rows = await self._execute(
                        combined[period_tag]["sql"], request.datasource,
                        purpose="driver_tree", period=period_tag,
                    )
                    if rows:
                        _fill(bucket, [k["resolved"] for k in kept], list(rows[0]))
            else:
                # 逐候选回退:组合编译失败(交互)时各自取数;预算内逐个跑,
                # 超预算的候选截断记账(宁缺勿错)
                for k in kept:
                    if self._queries + need > lim.max_queries:
                        degraded.append({"stage": "driver_tree",
                                         "reason": f"query_budget_exceeded:{k['candidate']}"[:120]})
                        break
                    if k["sql_cur"]:
                        cols, rows = await self._execute(
                            k["sql_cur"], request.datasource,
                            purpose="driver_tree", period="current",
                        )
                        if rows:
                            _fill(cur_vals, [k["resolved"]], list(rows[0]))
                    if k["sql_base"]:
                        cols, rows = await self._execute(
                            k["sql_base"], request.datasource,
                            purpose="driver_tree", period="base",
                        )
                        if rows:
                            _fill(base_vals, [k["resolved"]], list(rows[0]))
            self._mark_tree(root, comps, cur_vals, base_vals, alias=alias,
                            cur_total=cur_total, base_total=base_total)
        except Exception as e:
            logger.warning("Driver tree execution failed (%s); keeping skeleton", e)
            degraded.append({"stage": "driver_tree", "reason": str(e)[:200]})
            self._mark_tree(root, comps, {}, {}, alias=alias,
                            cur_total=cur_total, base_total=base_total)

        return _finish()

    def _mark_tree(
        self,
        root: dict[str, Any],
        comps: list[dict[str, Any]],
        cur_vals: dict[str, float],
        base_vals: dict[str, float],
        *,
        alias: dict[str, str] | None = None,
        cur_total: float = 0.0,
        base_total: float = 0.0,
    ) -> None:
        """组件值落树 + 诚实残差(就地改 root/comps 的节点字典)。

        值按 **alias(候选→解析后度量名)** 查:不同拼写指向同一个声明
        度量时共享值(组合查询按度量名去重,别名防止位置错位)。
        """
        alias = alias or {}

        def _value(bucket: dict[str, float], cand: str) -> float | None:
            if not cand:
                return None
            key = alias.get(cand, cand)
            if key in bucket:
                return bucket[key]
            return bucket.get(cand)

        for node in [root] + comps:
            cand = str(node.get("candidate") or "")
            cur_v = _value(cur_vals, cand)
            base_v = _value(base_vals, cand)
            if cur_v is not None or base_v is not None:
                node["current"] = cur_v if cur_v is not None else 0.0
                node["base"] = base_v if base_v is not None else 0.0
                node["delta"] = node["current"] - node["base"]
                node["executed"] = True
            else:
                node.setdefault("executed", False)

        # 根的口径是 hop0(与卡片头条一致),必须在 annotate **之前**落值:
        # 残差 = 根 Δ − 带符号子 Δ 之和,根没有值时它是 0.0 —— 一次精确的
        # 分解会被算成 gap(值还恰等于子 Δ 之和,读起来像真残差)。
        # 根被预检剔除(遮蔽/不可聚合)而 hop0 成功时就会走到这里。
        if self._hop0_ok:
            root["current"] = cur_total
            root["base"] = base_total
            root["delta"] = cur_total - base_total
            root["executed"] = True

        def annotate(node: dict[str, Any]) -> None:
            kids = node.get("children") or []
            for k in kids:
                annotate(k)
            if not kids:
                return
            decomposable = bool(node.get("decomposable"))
            if not decomposable:
                for k in kids:
                    k.setdefault("informational", True)
                node["residual"] = {"value": None, "exact": False, "reason": "non_decomposable"}
                return
            child_deltas = [k.get("delta") for k in kids]
            if any(d is None for d in child_deltas):
                node["residual"] = {"value": None, "exact": False, "reason": "component_unavailable"}
                return
            # 减法链按算子带符号:Δ(a−b)=Δa−Δb,直接求和会误判出残差。
            signed = signed_children(node.get("op"), [float(d) for d in child_deltas])
            res = residual(float(node.get("delta") or 0.0), signed)
            res["reason"] = "identity" if res["exact"] else "gap"
            node["residual"] = res

        annotate(root)

    # ── 聚焦组件分解(决策桥 / 未来 what-if)─────────────

    async def run_components(
        self, request: AnalysisRequest, *, include_total: bool = True,
    ) -> AnalysisOutcome | None:
        """根总量 + 指标组件树 —— 不跑维度探测、主分解与 drilldown。

        与 ``run()`` 的区别只是"少做什么":桥要回答的是「总量怎么变、哪些
        组件推动」,不需要维度预选与下钻。顺序刻意**树优先** —— 树是主产物,
        总量(hop0)只在"补上它也不挤占树"时才跑;树的根查询与 hop0 同源,
        缺它不过是根的 ``value_source`` 记为 ``tree_query``。反过来先跑
        hop0,在紧预算下会留下一个空树。

        适用门(不满足 → None,调用方静默跳过,这不是失败):指标可解析、
        表达式树确有组件、时间字段可解析、当前期可得。任何执行异常都不外抛
        —— 桥的失败只记 ``degraded``,绝不影响判定本身。
        """
        lim = self.limits
        metric_name = str(request.metric or "").strip()
        if not metric_name:
            return None
        dialect = request.dialect or "sqlite"
        matched = list(request.matched)
        datasource = request.datasource
        degraded: list[dict[str, Any]] = []

        metric_obj = resolve_metric(self._sl, metric_name)
        if metric_obj is None:
            return None
        try:
            model = self._sl.model()
        except Exception:
            model = None
        if model is None:
            return None

        root = metric_components(metric_obj, model, max_depth=lim.tree_max_depth)
        if not (root.get("children") or []):
            return None      # 单叶子:表达式不可分解,桥对这条规则不适用

        time_field = resolve_time_field(self._sl, matched, metric_name)
        if time_field is None:
            return None
        if request.periods is not None:
            cur_period, base_period = request.periods
        else:
            periods = _derive_periods(request.time_context or "", request.baseline)
            cur_period, base_period = (
                (periods[0], periods[1]) if periods else (None, None))
        if cur_period is None:
            return None

        cur_total = base_total = 0.0
        need = 2 if (cur_period and base_period) else 1
        if include_total and self._queries + 2 + need <= lim.max_queries:
            try:
                cur_sql = compile_hop(self._sl, matched, dialect, metric_name, [],
                                      time_conds(time_field, cur_period, dialect=dialect))
                base_sql = compile_hop(self._sl, matched, dialect, metric_name, [],
                                       time_conds(time_field, base_period, dialect=dialect))
                if cur_sql:
                    cols, rows = await self._execute(
                        cur_sql, datasource, purpose="overall",
                        period="current", keep=5)
                    cur_total = num(rows[0][-1]) if rows and rows[0] else 0.0
                    self._hop0_ok = True
                if base_sql and base_period:
                    cols, rows = await self._execute(
                        base_sql, datasource, purpose="overall",
                        period="base", keep=5)
                    base_total = num(rows[0][-1]) if rows and rows[0] else 0.0
            except Exception as e:
                degraded.append({"stage": "overall", "reason": str(e)[:200]})

        tree = await self._build_tree(
            request, metric_obj, cur_period, base_period, time_field,
            cur_total, base_total, degraded,
        )
        if tree is None:
            return None
        series = await self._series_stage(
            request, metric_name, time_field, cur_period, datasource,
            degraded, current=cur_total if self._hop0_ok else None,
        )

        outcome = AnalysisOutcome(
            metric=metric_name, baseline=request.baseline,
            cur_total=cur_total, base_total=base_total,
            total_delta=cur_total - base_total,
        )
        outcome.tree = tree
        outcome.series = series
        outcome.budget = self._budget_snapshot()
        outcome.hops = list(self._hops)
        outcome.evidence_queries = list(self._evidence)
        outcome.degraded = degraded
        outcome.partial = bool(degraded)
        return outcome

    # ── 主流程 ──────────────────────────────────────────

    async def run(self, request: AnalysisRequest) -> AnalysisOutcome | None:
        """执行一次分析;无可交付产物 → None(节点静默跳过,主链零影响)。"""
        lim = self.limits
        sl = self._sl
        metric_name = str(request.metric or "").strip()
        dims = [
            str(d).strip()
            for d in (request.dimensions or [])
            if str(d or "").strip()
        ][: lim.max_dimensions]
        if not metric_name or not dims:
            return None
        baseline = str(request.baseline or "prev_period").strip().lower()
        if baseline not in ("prev_period", "yoy", "share"):
            baseline = "prev_period"
        depth = min(int(request.depth or 1), lim.max_hops)
        focus = request.focus

        dialect = request.dialect or "sqlite"
        matched = list(request.matched)
        datasource = request.datasource
        degraded: list[dict[str, Any]] = []

        # 时间字段判定失败 / 时间窗解析不出 → baseline 降级 share(无基期,
        # 占比归因)。**降级必须说**:请求了 yoy/环比却拿不到两个可比窗口
        # 时静默降成占比,查询会在无时间过滤的数据上算「1997 vs 1996」——
        # 结论数字对不上口径,而 partial 是唯一能看见它的地方。
        time_field = resolve_time_field(sl, matched, metric_name)
        periods = None
        if time_field:
            periods = _derive_periods(request.time_context or "", baseline)
        if periods is None and baseline != "share":
            degraded.append({
                "stage": "period",
                "reason": (
                    "no_time_field" if not time_field
                    else "no_time_context"
                    if not str(request.time_context or "").strip()
                    else "unparsable_time_context"
                ),
            })
        if periods is None:
            baseline = "share"
        cur_period = periods[0] if periods else None
        base_period = periods[1] if periods else None

        # 维度字段解析(全部解析失败 → 静默跳过,不给编造维度)
        dim_refs: list[str] = []
        for d in dims:
            ref = resolve_dim_ref(sl, matched, d)
            if ref is None:
                break
            dim_refs.append(ref)
        if not dim_refs:
            return None
        # 计划维度名 ↔ 解析 ref 的映射(ref 可被 probe 重排,名字不可)
        ref_to_dim: dict[str, str] = dict(zip(dim_refs, dims))

        # 度量解析 + 比率判定(方向 2):metric_type=ratio / AVG / A÷B / SAFE_DIVIDE
        metric_obj = resolve_metric(sl, metric_name)
        ratio_parts: tuple[str, str] | None = None
        if lim.ratio_decomposition and metric_obj is not None:
            ratio_parts = metric_ratio_parts(metric_obj)
        is_ratio = ratio_parts is not None

        outcome = AnalysisOutcome(metric=metric_name, baseline=baseline)
        table: list[dict[str, Any]] = []
        effects: dict[str, Any] | None = None
        total_delta = 0.0
        base_total = cur_total = 0.0
        primary_dim = dims[0]
        drill_table: list[dict[str, Any]] = []
        series: dict[str, Any] | None = None
        try:
            # hop0:整体 Δ(无维度)——当前期 vs 基期总量对比
            cur_sql = compile_hop(
                sl, matched, dialect, metric_name, [],
                time_conds(time_field, cur_period, dialect=dialect),
            )
            base_sql = compile_hop(
                sl, matched, dialect, metric_name, [],
                time_conds(time_field, base_period, dialect=dialect),
            )
            if cur_sql:
                cols, rows = await self._execute(cur_sql, datasource, purpose="overall", period="current", keep=5)
                cur_total = num(rows[0][-1]) if rows and rows[0] else 0.0
                self._hops.append({"hop": 0, "sql": cur_sql, "columns": cols, "rows": _record_rows(rows, 5), "period": "current"})
                self._hop0_ok = True
            if base_sql and base_period:
                cols, rows = await self._execute(base_sql, datasource, purpose="overall", period="base", keep=5)
                base_total = num(rows[0][-1]) if rows and rows[0] else 0.0
                self._hops.append({"hop": 0, "sql": base_sql, "columns": cols, "rows": _record_rows(rows, 5), "period": "base"})
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
                lim.probe_dimensions
                and len(dim_refs) >= 2
                and base_period is not None
                and not focus_conds
            ):
                best_sig, best_ref = -1.0, d0_ref
                for ref in dim_refs[: lim.max_dimensions]:
                    cur_map, base_map, sig, hop_c, hop_b = await self._probe_dim(
                        matched, dialect, metric_name, metric_obj, ratio_parts, ref, [],
                        time_field, cur_period, base_period, datasource,
                    )
                    if sig > best_sig:
                        best_sig, best_ref = sig, ref
                        probe_cache = {
                            "ref": ref,
                            "cur": cur_map, "base": base_map,
                            "hop_cur": hop_c, "hop_base": hop_b,
                        }
                if best_ref != d0_ref:
                    # 重排:最佳维居首,其余保持计划顺序
                    dim_refs = [best_ref] + [r for r in dim_refs if r != best_ref]
                    d0_ref = dim_refs[0]
            # 记录实际主拆维度(可能被 probe 重排):ref 反查计划维度名
            primary_dim = ref_to_dim.get(d0_ref, dims[0])

            # hop1:按主拆维度分解(探测命中则复用;key 必须比 ref ——
            # 探到的总是 best_ref,d0_ref 重排后即它,命中即省两条查询)
            if probe_cache.get("ref") == d0_ref:
                cur_map = probe_cache["cur"]
                base_map = probe_cache["base"]
                if probe_cache.get("hop_cur"):
                    self._hops.append(probe_cache["hop_cur"])
                if probe_cache.get("hop_base"):
                    self._hops.append(probe_cache["hop_base"])
            else:
                cur_map, base_map, _sig, hop_c, hop_b = await self._probe_dim(
                    matched, dialect, metric_name, metric_obj, ratio_parts, d0_ref, focus_conds,
                    time_field, cur_period, base_period, datasource,
                )
                if hop_c:
                    self._hops.append(hop_c)
                if hop_b:
                    self._hops.append(hop_b)

            if is_ratio:
                if base_period is not None and base_map:
                    dec = shift_share(base_map, cur_map)
                    table = dec["rows"]
                    effects = dec["effects"]
                    base_total = dec["base_total"]
                    cur_total = dec["cur_total"]
                    total_delta = dec["effects"]["delta"]
                else:
                    dec = ratio_share(cur_map)
                    table = dec["rows"]
                    cur_total = dec["cur_total"]
                    base_total = 0.0
            else:
                table = contribution(base_map, cur_map)

            # hop2:下钻(depth>=2 且还有第二个维度)——对 top |contribution|
            # 项加过滤后按 dimensions[1] 再分解(比率指标同样 shift-share)。
            if depth >= 2 and len(dim_refs) >= 2:
                top = table[0] if table else None
                # 下钻信号用 contribution(比率指标率变化可为 0 但权重移动贡献非 0)
                if top and top["contribution"] != 0:
                    d1_ref = dim_refs[1]
                    drill_conds = [{"field": d0_ref, "op": "=", "value": str(top["dim"])}]
                    if is_ratio:
                        cur_sql = compile_ratio_hop(
                            sl, matched, dialect, metric_obj, ratio_parts,
                            d1_ref, drill_conds + time_conds(time_field, cur_period, dialect=dialect),
                        )
                        base_sql = (
                            compile_ratio_hop(
                                sl, matched, dialect, metric_obj, ratio_parts,
                                d1_ref, drill_conds + time_conds(time_field, base_period, dialect=dialect),
                            )
                            if base_period else None
                        )
                        drill_cur: dict[str, Any] = {}
                        drill_base: dict[str, Any] = {}
                        if cur_sql:
                            cols, rows = await self._execute(cur_sql, datasource, purpose="drilldown", period="current", filt=str(top["dim"]))
                            drill_cur = rows_to_numden(cols, rows)
                            self._hops.append({"hop": 2, "sql": cur_sql, "columns": cols, "rows": _record_rows(rows, 10), "period": "current", "filter": str(top["dim"])})
                        if base_sql:
                            cols, rows = await self._execute(base_sql, datasource, purpose="drilldown", period="base", filt=str(top["dim"]))
                            drill_base = rows_to_numden(cols, rows)
                            self._hops.append({"hop": 2, "sql": base_sql, "columns": cols, "rows": _record_rows(rows, 10), "period": "base", "filter": str(top["dim"])})
                        drill_table = shift_share(drill_base, drill_cur)["rows"]
                    else:
                        cur_sql = compile_hop(
                            sl, matched, dialect, metric_name, [d1_ref],
                            drill_conds + time_conds(time_field, cur_period, dialect=dialect),
                        )
                        base_sql = compile_hop(
                            sl, matched, dialect, metric_name, [d1_ref],
                            drill_conds + time_conds(time_field, base_period, dialect=dialect),
                        )
                        drill_cur_v: dict[str, float] = {}
                        drill_base_v: dict[str, float] = {}
                        if cur_sql:
                            cols, rows = await self._execute(cur_sql, datasource, purpose="drilldown", period="current", filt=str(top["dim"]))
                            drill_cur_v = rows_to_map(cols, rows)
                            self._hops.append({"hop": 2, "sql": cur_sql, "columns": cols, "rows": _record_rows(rows, 10), "period": "current", "filter": str(top["dim"])})
                        if base_sql and base_period:
                            cols, rows = await self._execute(base_sql, datasource, purpose="drilldown", period="base", filt=str(top["dim"]))
                            drill_base_v = rows_to_map(cols, rows)
                            self._hops.append({"hop": 2, "sql": base_sql, "columns": cols, "rows": _record_rows(rows, 10), "period": "base", "filter": str(top["dim"])})
                        drill_table = contribution(drill_base_v, drill_cur_v)

            self._produced = True

            # 块序列(统计器械;独立门控,失败只降级)
            series = await self._series_stage(
                request, metric_name, time_field, cur_period, datasource,
                degraded, current=cur_total if self._hop0_ok else None,
            )

            # 驱动器树(新阶段;独立门控,失败只降级)
            tree = None
            if lim.driver_tree:
                tree = await self._build_tree(
                    request, metric_obj, cur_period, base_period, time_field,
                    cur_total, base_total, degraded,
                )
        except Exception as e:
            logger.warning("Attribution analysis failed (%s); degrading to partial hops", e)
            degraded.append({"stage": "analysis", "reason": str(e)[:200]})
            if not self._produced and self._hops:
                return AnalysisOutcome(
                    metric=metric_name, baseline=baseline, hops=list(self._hops),
                    evidence_queries=list(self._evidence), degraded=degraded,
                    partial=True, degraded_result=True,
                )
            tree = None
            series = None

        if not self._hops:
            return None

        outcome.table = table
        outcome.effects = effects
        outcome.total_delta = total_delta
        outcome.base_total = base_total
        outcome.cur_total = cur_total
        # 聚合守恒自查(设计 2026-10-09 §2.2):加性分解的和必须等于总变化。
        # **顺序是承重的** —— 必须在 degraded → partial 赋值(:1227-1228)之前,
        # 违例才会把 partial 置真(产物不自洽 = 这次分析按部分降级呈现)。
        # 只查加性分支:比率路径的守恒由 shift_share 结构自带,重算只会假警。
        if not is_ratio and base_period is not None:
            gap = conservation_gap(table, total_delta)
            if gap is not None and conservation_violated(gap, total_delta):
                degraded.append({
                    "stage": "conservation",
                    "reason": f"decomposition sum != total_delta (gap={gap:.6g})",
                })
        outcome.primary_dim = primary_dim
        outcome.dimensions = [primary_dim] + [d for d in dims if d != primary_dim]
        outcome.is_ratio = is_ratio
        outcome.hops = list(self._hops)
        outcome.evidence_queries = list(self._evidence)
        outcome.degraded = degraded
        outcome.partial = bool(degraded)
        if drill_table:
            drill_dim = (
                ref_to_dim.get(dim_refs[1], dims[1])
                if len(dim_refs) >= 2 else primary_dim
            )
            outcome.drilldown = {"dimension": drill_dim, "table": drill_table}
        outcome.tree = tree
        outcome.series = series
        outcome.budget = self._budget_snapshot()
        return outcome


# ── payload 组装(纯函数)─────────────────────────────────

def analysis_payload(
    outcome: AnalysisOutcome,
    *,
    question: str,
    chart: dict[str, Any] | None,
    baseline_label: str,
    datasource: str,
) -> dict[str, Any]:
    """AnalysisOutcome → ``state.analysis``(前端/回放/缓存同源同键)。"""
    if outcome.tree and outcome.table:
        kind = "combined"
    elif outcome.tree:
        kind = "driver_tree"
    else:
        kind = "attribution"
    truncated = any(bool(q.get("truncated")) for q in outcome.evidence_queries)
    payload = {
        # v2 = v1 形状 + **全可选**的统计节(series 含噪声带/z/位置分数;
        # evidence.budget)+ 节点层附录(hypotheses,attribution 节点在
        # analysis_payload 之后附加;分析包本体永远零 LLM)。
        # 版本描述**生产者 schema**,不由本次 payload 恰好带了哪些节决定 ——
        # 按内容变版本会让同一个生产者在两个版本间摇摆(migrations 同款
        # 纪律:版本反映历史,从不反映配置)。兼容机制 = **缺席容忍**:
        # 不认识的新键原样忽略,v1 payload 无需迁移(前端类型全可选 +
        # 「拿不到不整节渲染」)。
        # 边界:判定侧的 significance/causal 证据节**不进**本 payload ——
        # 它们属于 verdict.evidence(判定记录自带证据),这里不复制第二份;
        # 分析侧对应的统计节就是 series(噪声带即分析侧的显著性)。
        "version": 2,
        "kind": kind,
        "metric": outcome.metric,
        "metric_kind": "ratio" if outcome.is_ratio else "additive",
        "labels": {
            "question": (question or "").strip()[:60],
            "baseline": outcome.baseline,
            "baseline_label": baseline_label,
            "primary_dimension": outcome.primary_dim,
            "dimensions": list(outcome.dimensions),
        },
        "total_delta": outcome.total_delta,
        "table": outcome.table,
        "effects": outcome.effects,
        "drilldown": outcome.drilldown,
        "tree": outcome.tree,
        "charts": [chart] if chart else [],
        "evidence": {
            "datasource": datasource,
            "queries": outcome.evidence_queries,
            "truncated": truncated,
            "degraded": outcome.degraded,
        },
        "partial": outcome.partial,
    }
    # 新节只在有值时出现:老路径(序列/预算未启用)payload 逐字节不变
    if outcome.series is not None:
        payload["series"] = outcome.series
    if outcome.budget is not None:
        payload["evidence"]["budget"] = outcome.budget
    # 出门前整包安全化(见 ``core.serialize``):记录视图已在源头逐格化
    # (``_record_rows``),这里再兜一层是因为**标签**也可能是 DB 原生值
    # (日期维度 → ``date`` 键,树/系列/贡献表的 dim 同理)—— 这份 payload
    # 的契约就是 JSON(前端/回放/缓存同源同键),契约要在出口成立。
    return json_safe(payload)
