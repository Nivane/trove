"""Semantic query API builder — declarative metric/dimension requests.

Turns a REST body (``metrics`` / ``dimensions`` / ``time_grain`` / ``filters``
/ ``order_by`` / ``limit``) into the typed query plan and compiles it through
the authoritative ``SemanticCompiler`` — the same logical universe the NL
pipeline compiles against. This is the standalone semantic query surface
(MetricFlow Query API style): read-only, deterministic, guardrailed, and
independent of LLM generation.

Any component that does not resolve to a declared model entry raises
``SemanticQueryError`` (the router maps it to a 422). Nothing is guessed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trove.services.semantic_layer.compiler import (
    CompileMiss,
    SemanticCompiler,
    _agg_signature,
    _sig_compatible,
    validate_compiled_sql,
)
from trove.services.semantic_layer.models import SemanticMetric, SemanticModel
from trove.services.semantic_layer.plan import GRAINS


class SemanticQueryError(ValueError):
    """Invalid or undeclared query component — safe to surface to callers."""


@dataclass
class SemanticQuery:
    """One declarative semantic query request."""

    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    time_grain: dict[str, str] | None = None
    filters: list[dict[str, Any]] = field(default_factory=list)
    order_by: list[dict[str, Any]] = field(default_factory=list)
    limit: int | None = None


def _resolve_metric(model: SemanticModel, candidate: str) -> SemanticMetric:
    """候选 → 声明度量:先按名精确匹配,再按聚合签名兼容(同编译器)。"""
    cand = str(candidate).strip()
    for m in model.metrics:
        if m.name.strip().lower() == cand.lower():
            return m
    sig = _agg_signature(cand)
    if sig is not None:
        for m in model.metrics:
            msig = _agg_signature(m.expression)
            if msig is not None and _sig_compatible(sig, msig):
                return m
    raise SemanticQueryError(f"metric not declared: {candidate!r}")


def _resolve_field(model: SemanticModel, ref: str) -> tuple[str, Any]:
    """字段引用 → (dataset.name, field);裸列名须跨数据集唯一解析。"""
    ref = str(ref).strip()
    if not ref:
        raise SemanticQueryError("field reference must not be empty")
    if "." in ref:
        ds_name, fname = ref.split(".", 1)
        ds = next(
            (d for d in model.datasets if d.name.lower() == ds_name.strip().lower()),
            None,
        )
        if ds is None:
            raise SemanticQueryError(f"dataset not declared: {ds_name!r}")
        f = next(
            (x for x in ds.fields if x.name.lower() == fname.strip().lower()), None)
        if f is None:
            raise SemanticQueryError(f"field not declared: {ref!r}")
        return ds.name, f
    fname = ref
    hits = [
        (d.name, x)
        for d in model.datasets
        for x in d.fields
        if x.name.lower() == fname.lower()
    ]
    if not hits:
        raise SemanticQueryError(f"field not declared: {ref!r}")
    if len(hits) > 1:
        raise SemanticQueryError(
            f"ambiguous field {ref!r} — qualify it as dataset.field")
    return hits[0]


def build_and_compile(
    model: SemanticModel,
    query: SemanticQuery,
    dialect: str = "sqlite",
    matched: list[str] | None = None,
) -> dict[str, Any]:
    """声明式查询 → 权威 SQL + 输出列 + 锚定数据集。

    Returns:
        ``{"sql", "columns", "datasets", "version"}``.

    Raises:
        SemanticQueryError: 任一组件未声明/不可解析(严格,不猜测)。
    """
    if model is None:
        raise SemanticQueryError("no semantic model")

    metrics = [_resolve_metric(model, c) for c in (query.metrics or [])]
    if not metrics:
        raise SemanticQueryError("at least one metric is required")

    dims: list[str] = []
    dim_datasets: list[str] = []
    for d in query.dimensions or []:
        ds, f = _resolve_field(model, d)
        dims.append(f"{ds}.{f.name}")
        dim_datasets.append(ds)

    tg: dict[str, str] | None = None
    tg_datasets: list[str] = []
    if query.time_grain is not None:
        grain = str(query.time_grain.get("grain") or "").strip().lower()
        if grain not in GRAINS:
            raise SemanticQueryError(f"unknown time grain: {grain!r}")
        ds, f = _resolve_field(model, str(query.time_grain.get("field") or ""))
        tg = {"field": f"{ds}.{f.name}", "grain": grain}
        tg_datasets.append(ds)

    # 锚定数据集:metric 数据集优先(按序去重),其次维度/时间字段数据集
    anchor: list[str] = []
    for m in metrics:
        for ds in m.datasets:
            if ds and ds not in anchor:
                anchor.append(ds)
    for ds in dim_datasets + tg_datasets:
        if ds and ds not in anchor:
            anchor.append(ds)
    if not anchor:
        raise SemanticQueryError(
            "cannot determine anchor datasets — metrics have no dataset anchor")

    matched = matched or anchor

    conditions: list[dict[str, Any]] = []
    for f in query.filters or []:
        if not isinstance(f, dict):
            raise SemanticQueryError("each filter must be an object")
        field_ref = str(f.get("field") or "").strip()
        if not field_ref:
            raise SemanticQueryError("filter requires a field")
        _resolve_field(model, field_ref)
        if "value" not in f:
            raise SemanticQueryError(f"filter on {field_ref} requires a value")
        conditions.append({
            "field": field_ref,
            "op": str(f.get("op") or "=").strip(),
            "value": f.get("value"),
        })

    ordering: list[dict[str, str]] = []
    for o in query.order_by or []:
        col = str(o.get("column") or "").strip()
        if not col:
            raise SemanticQueryError("order_by requires a column")
        ordering.append({"column": col, "direction": str(o.get("direction") or "asc")})

    plan: dict[str, Any] = {
        "tables": list(matched),
        "answer_columns": dims + [m.name for m in metrics],
        "conditions": conditions,
        "ordering": ordering,
        "limit": query.limit,
    }
    if tg:
        plan["time_grain"] = tg

    result = SemanticCompiler(model).compile_detailed(
        plan, list(matched), force_dialect=dialect or "sqlite")
    if isinstance(result, CompileMiss):
        raise SemanticQueryError(
            f"compilation failed: {result.reason} ({result.component})")
    violations = validate_compiled_sql(result.sql, model, list(matched))
    if violations:
        raise SemanticQueryError("guardrail rejected: " + "; ".join(violations))

    columns = [d.rsplit(".", 1)[-1] for d in dims]
    if tg:
        columns.append(str(tg["grain"]))
    columns += [m.name for m in metrics]
    return {
        "sql": result.sql,
        "columns": columns,
        "datasets": list(matched),
        "version": model.version or "",
    }
