"""L3 semantic drift — do the model's declarations still hold on live data?

L1 asks whether the tables/columns a KB describes still exist. L2 asks
whether the semantic model's *references* resolve. Neither answers the
question this module answers: **are the declared claims true?**

Two families, both read-only and zero-LLM:

- **Value anchoring** — a field's ``values`` snapshot says which literals
  exist. If a declared value is gone from the live column, a filter on it
  silently returns zero rows; that is drift, and it is invisible at runtime.
  Extra live values are *not* drift (the snapshot is merely stale), so the
  comparison is deliberately one-directional.
- **Relationship additivity** — a declared key that duplicates, a ``1:N``
  edge whose "one" side is not unique, or a ``fan_out: dedup`` exemption
  whose duplicates are not exact rows. These are already probed by
  :mod:`trove.services.semantic_layer.verify`; this module only maps its
  verdicts into the drift vocabulary instead of writing a second verifier.

**判不了 ≠ 通过.** Probes that cannot run (unsupported dialect, non-physical
field expression, domain too wide, SQL failure) are returned in a separate
``unverified`` list. They never become items, and the merge layer must not
count the level as verified when the list is non-empty.

The detector opens no connections: the adapter is injected, exactly like
``verify_declarations`` and the analysis engine's runner.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from trove.services.semantic_layer.provider import _BARE_COL_RE
from trove.services.semantic_layer.verify import (
    UNVERIFIABLE,
    VIOLATED,
    _PROBE_QUOTES,
    _q,
    _table_ref,
    verify_declarations,
)

LEVEL = "L3"

#: 值快照的探针上限(与 enum_probe 的 MAX_FIELD_VALUES 同量级)。超过它
#: 不猜「值还在不在」——只报一条 unverified,而不是把域太宽读成缺失。
_VALUE_PROBE_LIMIT = 100

#: 一个字段值域里的活值上限;声明值本身超过 100 时按声明数放宽。
_VALUE_DETAIL_CAP = 100

_VALUE_MISSING = "value_missing"
_KEY_NOT_UNIQUE = "key_not_unique"
_RELATION_NOT_ADDITIVE = "relationship_not_additive"
_FAN_OUT_NOT_DEDUPLICABLE = "fan_out_not_deduplicable"

_VERIFY_KIND_MAP = {
    "keys": (_KEY_NOT_UNIQUE, "critical"),
    "relationships": (_RELATION_NOT_ADDITIVE, "critical"),
    "fan_out": (_FAN_OUT_NOT_DEDUPLICABLE, "critical"),
}


def _item(kind: str, subject: str, severity: str, detail: dict) -> dict:
    return {
        "level": LEVEL,
        "kind": kind,
        "subject": subject,
        "severity": severity,
        "detail": detail,
    }


def _unverified(subject: str, reason: str) -> dict:
    return {"level": LEVEL, "subject": subject, "reason": reason}


def _canon(raw: Any) -> str:
    return "" if raw is None else str(raw).strip()


def _same_value(declared: Any, live: Any) -> bool:
    """声明的字面量是否仍然存在于活库值里。

    三种等价,刻意都很窄:精确、数值、大小写。不做相似度、不做翻译——
    把 ``male`` 猜成 ``M`` 会让「值消失」这条信号变成一个静默的假通过。
    大小写是为 MySQL 的 CI 排序规则留的;``Decimal`` 是为数值快照以字符串
    落盘(``"10"`` vs ``10``)留的。
    """
    a, b = _canon(declared), _canon(live)
    if a == b or a.casefold() == b.casefold():
        return True
    try:
        return Decimal(a) == Decimal(b)
    except (InvalidOperation, ValueError, TypeError):
        return False


def _field_target(ds_name: str, field_name: str) -> str:
    return f"{ds_name}.{field_name}"


async def _probe_values(adapter, model, dialect: str) -> tuple[list[dict], list[dict]]:
    """声明的 ``values`` 是否仍能在活库列里找到。"""
    items: list[dict] = []
    unverified: list[dict] = []
    quote = _PROBE_QUOTES.get(dialect)
    if quote is None:
        # verify_declarations 已经会为整个模型产出唯一的一条诚实跳过;
        # 值探针不再重复刷屏。
        return items, unverified

    for ds in model.datasets:
        for f in ds.fields:
            declared = [v for v in dict.fromkeys(f.values) if _canon(v)]
            if not declared:
                continue
            target = _field_target(ds.name, f.name)
            expr = str(f.expression or "").strip()
            if not _BARE_COL_RE.match(expr):
                unverified.append(_unverified(
                    target,
                    "field_expression_not_physical: 字段表达式不是裸列名,"
                    "无法确定物理列"))
                continue
            if not ds.source:
                unverified.append(_unverified(
                    target,
                    "dataset_source_missing: 数据集未声明物理来源(source)"))
                continue

            cap = max(len(declared), _VALUE_PROBE_LIMIT)
            table = _table_ref(ds.source, quote)
            col = _q(expr, quote)
            try:
                rows = (await adapter.execute(
                    f"SELECT DISTINCT {col} FROM {table} LIMIT {cap + 1}"
                )).rows
            except Exception as e:  # noqa: BLE001 — 探测失败如实报,不当通过
                unverified.append(_unverified(
                    target, f"value_probe_failed: {e}"))
                continue

            live = [r[0] for r in rows if r and r[0] is not None]
            if len(live) > cap:
                unverified.append(_unverified(
                    target,
                    f"value_domain_too_large: 活值超过 {cap} 个,"
                    f"不猜声明值是否还在"))
                continue

            missing = [
                str(v) for v in declared
                if not any(_same_value(v, live_value) for live_value in live)
            ]
            if missing:
                items.append(_item(
                    _VALUE_MISSING, target, "warning",
                    {
                        "dataset": ds.name,
                        "field": f.name,
                        "expression": expr,
                        "missing": missing[:_VALUE_DETAIL_CAP],
                        "missing_count": len(missing),
                        "declared": len(declared),
                        "live_distinct": len(live),
                    },
                ))
    return items, unverified


async def _probe_relationships(adapter, model, dialect: str) -> tuple[list[dict], list[dict]]:
    """复用 verify 的关系/键/fan_out 探针,映射成漂移词汇。"""
    items: list[dict] = []
    unverified: list[dict] = []
    probes = await verify_declarations(adapter, model, dialect)
    for probe in probes:
        if probe.verdict == UNVERIFIABLE:
            subject = probe.target or model.name or "semantic_model"
            unverified.append(_unverified(
                subject, f"{probe.kind}: {probe.message}"))
            continue
        if probe.verdict != VIOLATED:
            # VERIFIED / ADVISORY / coverage 都不是「声明为假」——
            # advisory 是「值得看一眼」,不是漂移。
            continue
        mapped = _VERIFY_KIND_MAP.get(probe.kind)
        if mapped is None:
            continue
        kind, severity = mapped
        subject = probe.target or model.name or "semantic_model"
        items.append(_item(
            kind, subject, severity,
            {
                "probe": probe.kind,
                "target": probe.target,
                "message": probe.message,
            },
        ))
    return items, unverified


async def detect_live_drift(adapter, model, dialect: str) -> dict:
    """One L3 pass over one semantic model. Read-only; never raises for a
    probe failure (those are ``unverified`` entries)."""
    value_items, value_unverified = await _probe_values(adapter, model, dialect)
    rel_items, rel_unverified = await _probe_relationships(adapter, model, dialect)
    return {
        "status": "ok",
        "skip_reason": None,
        "items": value_items + rel_items,
        "unverified": value_unverified + rel_unverified,
    }
