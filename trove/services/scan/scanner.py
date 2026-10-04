"""确定性扫描器 —— 「谁超出了它自己的历史噪声带」。

一个扫描单元 = (metric, dim)(dim 为空 = 整度量)。每单元**一条**查询:
把窗口前的历史块与当期块一次取回(``(维度, bucket, 度量)`` 或
``(bucket, 度量)``),按维度值切序列,历史块进 ``stats.band``,当期块与带
比对 —— ``outside`` 为 True 即 anomaly。

三条纪律,与统计器械(stats.py)同源:

  1. **当期块不进噪声带**:带只由历史块构成,把被测点混进分布会自我
     稀释(自己把自己拉回带内);
  2. **算不出 ≠ 没问题**:未声明度量/维度、无时间字段、窗口与粒度不
     对齐、空序列、编译 MISS、预算让路 —— 全部落成 ``unverifiable``
     行,带 ``reason``。静默少一行扫描,和「今天一切正常」从产物上
     看没有区别;
  3. **零 LLM、零 hash()**:id/排序全确定性,同一份数据两次扫描给出
     逐字节相同的发现顺序(草稿的幂等判定依赖这一点)。
"""

from __future__ import annotations

from typing import Any

from trove.services.analysis.budget import QueryLedger
from trove.services.analysis.engine import (
    resolve_dim_ref,
    resolve_metric,
    resolve_time_field,
    time_conds,
)
from trove.services.analysis.series import (
    block_windows,
    compile_series_hop,
    derive_grain,
    same_phase_blocks,
    series_by_dim,
    series_from_rows,
)
from trove.services.analysis.stats import band, low_n, outside, robust_z
from trove.services.scan.models import Finding, ScanSpec

#: 证据里保留的结果行(供人复核;完整结果由 SQL 本身可复算)。
EVIDENCE_KEEP_ROWS = 5

HopRunner = Any  # async (sql, datasource) -> (columns, rows)


def _label_in(label: str, window: tuple[str, str]) -> bool:
    """bucket 标签是否落在窗口内(前缀比较,长度按标签截齐)。

    与 ``analysis.engine._series_stage`` 的 same_phase 过滤同一手法:
    sqlite 的月桶标签是 ``'YYYY-MM'``,直接和 ``'YYYY-MM-DD'`` 边界比较
    会因前缀短而全部漏掉。
    """
    n = len(label)
    return window[0][:n] <= label <= window[1][:n]


def _in_any_block(label: str, blocks: list[tuple[str, str]]) -> bool:
    return any(_label_in(label, b) for b in blocks)


def rank_findings(findings: list[Finding]) -> list[Finding]:
    """确定性排序:anomaly 在前(按 |z| 降序),unverifiable 在后(按名字)。

    |z| 是判据强度的排序键;``z`` 为 None(退化带)的 anomaly 排在数值
    z 之后但仍属 anomaly。同 |z| 时按 (metric, dimension, value) 定序 ——
    与输入顺序无关,同一份数据两次扫描必须给出同一份序列。
    """
    def key(f: Finding) -> tuple:
        is_anom = 0 if f.kind == "anomaly" else 1
        z = abs(f.z) if isinstance(f.z, (int, float)) else -1.0
        return (is_anom, -z, f.metric, f.dimension, f.value)

    return sorted(findings, key=key)


def _unverifiable(
    metric: str, dim: str, spec: ScanSpec, reason: str, *,
    value: str = "", sql: str = "",
) -> Finding:
    return Finding(
        metric=metric, dimension=dim, value=value, kind="unverifiable",
        reason=reason, window=spec.window, grain=spec.grain, mode=spec.mode,
        sql=sql,
    )


def spec_issues(semantic_layer: Any, spec: ScanSpec) -> list[str]:
    """规格引用在语义层里解析不过的问题清单(**写时校验**;空 = 可用)。

    与 ``scan_unit`` 逐条对齐(度量/维度/时间字段/锚定数据集),但无时间
    字段这一条在写时也拦:**扫描的判据是块序列**,没有时间字段的度量
    从来给不出块 —— 一个注定每次 tick 都只产 unverifiable 的规格不该被
    写进任务(错误在创建时说得清,在运行记录里说不清)。
    """
    issues: list[str] = []
    for metric_name, dim_name in spec.units:
        metric = resolve_metric(semantic_layer, metric_name)
        if metric is None:
            issues.append(f"unknown metric {metric_name!r}")
            continue
        matched = [str(d) for d in (getattr(metric, "datasets", None) or []) if str(d)]
        if not matched:
            issues.append(f"metric {metric_name!r} has no anchor dataset")
            continue
        if not resolve_time_field(semantic_layer, matched, metric_name):
            issues.append(
                f"metric {metric_name!r} has no time field "
                "(a block series cannot be built)")
        if dim_name and not resolve_dim_ref(semantic_layer, matched, dim_name):
            issues.append(
                f"unknown dimension {dim_name!r} for metric {metric_name!r}")
    return issues


async def scan_unit(
    *,
    semantic_layer: Any,
    runner: HopRunner,
    datasource: str,
    dialect: str,
    spec: ScanSpec,
    metric_name: str,
    dim_name: str,
    cur_window: tuple[str, str] | None,
    ledger: QueryLedger,
    evidence: list[dict[str, Any]],
) -> list[Finding]:
    """扫一个 (metric, dim) 单元 → 发现列表(0..n 行,但**从不**静默少一行)。

    返回空列表 = 该单元算得出、且没有组超带(正常的多数情况;单元数由
    调用方在报告里记账)。算不出 → 恰好一行 ``unverifiable``。
    """
    if cur_window is None:
        return [_unverifiable(metric_name, dim_name, spec, "no_window")]
    metric = resolve_metric(semantic_layer, metric_name)
    if metric is None:
        return [_unverifiable(metric_name, dim_name, spec, "unknown_metric")]
    matched = [str(d) for d in (getattr(metric, "datasets", None) or []) if str(d)]
    if not matched:
        return [_unverifiable(metric_name, dim_name, spec, "no_anchor_dataset")]
    time_field = resolve_time_field(semantic_layer, matched, metric_name)
    if not time_field:
        return [_unverifiable(metric_name, dim_name, spec, "no_time_field")]
    dim_ref = ""
    if dim_name:
        dim_ref = resolve_dim_ref(semantic_layer, matched, dim_name) or ""
        if not dim_ref:
            return [_unverifiable(metric_name, dim_name, spec, "unknown_dimension")]

    grain = str(spec.grain or "").strip() or derive_grain(cur_window)
    if grain is None:
        return [_unverifiable(metric_name, dim_name, spec, "grain_unaligned")]
    blocks = (
        same_phase_blocks(cur_window, int(spec.lookback))
        if spec.mode == "same_phase"
        else block_windows(cur_window, grain, int(spec.lookback))
    )
    if not blocks:
        return [_unverifiable(metric_name, dim_name, spec, "no_blocks")]

    if not ledger.can(1):
        ledger.yield_("scan", needed=1)
        return [_unverifiable(metric_name, dim_name, spec, "query_budget_exceeded")]

    # 一条查询覆盖「历史块 + 当期窗口」:块按 grain 生成,窗口尾一并取回,
    # 标签切分决定谁进带、谁是当期(same_phase 的稀疏块同样由标签筛出)。
    span = (blocks[0][0], max(blocks[-1][1], cur_window[1]))
    sql = compile_series_hop(
        semantic_layer, matched, dialect, metric_name,
        time_conds(time_field, span, dialect=dialect),
        time_grain=grain, time_field=time_field,
        dimensions=[dim_ref] if dim_ref else None,
    )
    if sql is None:
        return [_unverifiable(metric_name, dim_name, spec, "compile_miss")]
    try:
        cols, rows = await runner(sql, datasource)
    except Exception as e:  # noqa: BLE001 — 执行失败是一行 unverifiable,不是崩
        return [_unverifiable(metric_name, dim_name, spec, f"query_failed: {e}"[:200],
                              sql=sql)]
    ledger.record("scan")
    evidence.append({
        "purpose": "scan_series",
        "metric": metric_name,
        "dimension": dim_name,
        "sql": sql,
        "columns": list(cols),
        "row_count": len(rows),
        "rows": [list(r) for r in rows[:EVIDENCE_KEEP_ROWS]],
        "truncated": len(rows) > EVIDENCE_KEEP_ROWS,
    })

    if dim_ref:
        groups = series_by_dim(cols, rows)
    else:
        groups = {"": series_from_rows(cols, rows)} if rows else {}
    if not groups:
        return [_unverifiable(metric_name, dim_name, spec, "empty_series", sql=sql)]

    anomalies: list[Finding] = []
    issues: dict[str, int] = {}
    for dim_value in sorted(groups):
        pairs = groups[dim_value]
        history = [v for lbl, v in pairs if _in_any_block(lbl, blocks)]
        current_pairs = [(lbl, v) for lbl, v in pairs if _label_in(lbl, cur_window)]
        if not history:
            issues["no_history_blocks"] = issues.get("no_history_blocks", 0) + 1
            continue
        if not current_pairs:
            # 缺失 ≠ 0:当期无行可能是数据未到/未落,不是"掉到零"的证据。
            issues["no_current_block"] = issues.get("no_current_block", 0) + 1
            continue
        current_label, current = current_pairs[-1]
        b = band(history, k=float(spec.k))
        z = robust_z(current, history)
        hit = outside(current, b)
        if hit is None:
            issues["band_unavailable"] = issues.get("band_unavailable", 0) + 1
            continue
        if not hit:
            continue  # 判过了、在带内 —— 正常单元,不占发现位
        anomalies.append(Finding(
            metric=metric_name, dimension=dim_name, value=dim_value,
            kind="anomaly", window=spec.window,
            period=[current_label, current_label],
            current=current, band=b.to_dict(), z=z, outside=True,
            n=len(history), low_n=low_n(len(history)),
            grain=grain, mode=spec.mode, sql=sql,
        ))
    if anomalies:
        # 有超带发现时无异常的判不了组记进证据节(不丢,只是不占发现位)
        if issues:
            evidence[-1]["notes"] = [
                {"reason": r, "groups": n} for r, n in sorted(issues.items())]
        return anomalies
    if issues:
        # 整单元判不了:一行 unverifiable;多组时 reason 带分组计数(诚实且可读)
        if len(issues) == 1 and sum(issues.values()) == 1:
            reason = next(iter(issues))
        else:
            detail = "; ".join(f"{r}×{n}" for r, n in sorted(issues.items()))
            reason = f"{detail} ({sum(issues.values())}/{len(groups)} values)"
        return [_unverifiable(metric_name, dim_name, spec, reason, sql=sql)]
    return []


def cap_findings(findings: list[Finding], top_k: int) -> tuple[list[Finding], int]:
    """排名后截断 —— **只截 anomaly**,unverifiable 一行不丢。

    top_k 是对付提案洪水的闸(扫描一次可能吐出几十条超带),截掉的是
    「还不值得人看」的发现,不是「说不清」的发现:后者数量由扫描单元数
    封顶(metric × dim),保留它才是「判不了要看得见」的兑现。
    """
    ranked = rank_findings(findings)
    anomalies = [f for f in ranked if f.kind == "anomaly"]
    others = [f for f in ranked if f.kind != "anomaly"]
    kept = anomalies[: max(int(top_k), 0)]
    return kept + others, len(anomalies) - len(kept)
