"""两个现有检测器 → :class:`DriftItem` 的适配层(方案 B)。

**为什么是适配而不是重写**:两个检测器都写得干净、零 LLM、有缓存、有测试
覆盖,重写它们是把已验证的逻辑换成未验证的。真正缺的是**它们的输出无法
相加** —— 一边是 ``column_changes[table].added``,一边是 ``missing_fields
[dataset]``,于是 API / CLI / 门禁 / 影响面各自为每一种形状写一遍。适配层
是那个"只写一遍"的地方。

**原报告不丢**:归一掉的结构全部进 ``detail``,``relationship_breaks`` 的
人类可读 ``detail`` 字符串原样保留。排查事故时要的往往正是被归一掉的上下文。

**级别归属**(为什么这些是 L1 那些是 L2):判据是「哪份声明被违反了」。

- **L1** 违反的是 **KB ``schema_notes.yml``** —— 它是*描述*,喂给提示词。
  失配的后果是模型可能写到不存在的表上,查询失败但**不会静默答错**。
- **L2** 违反的是 **``semantics.yml``** —— 它是*契约*,直接参与编译。
  失配的后果是编译产物引用不存在的对象,**答案本身失效**。

所以同为"表没了",L1 记 ``warning``、L2 记 ``critical``:不是严重程度的主观
排序,是「违反了描述还是违反了契约」的客观区分。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from trove.services.drift.models import (
    L1,
    L2,
    RUN_OK,
    RUN_SKIPPED,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    DriftItem,
    DriftReport,
    normalize_subject,
)

#: 检测器未带 ``status`` 时按「已检测」处理 —— 兼容任何仍返回旧形状(无
#: status 键)的第三方/测试替身。真实检测器自 2026-09-28 起必带该字段。
_ASSUME_CHECKED = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_of(report: dict[str, Any]) -> tuple[str, str | None]:
    """从检测器报告里取出 (status, skip_reason)。"""
    if "status" not in report:
        return (RUN_OK, None) if _ASSUME_CHECKED else (RUN_SKIPPED, "unknown")
    status = str(report.get("status") or RUN_OK)
    reason = report.get("skip_reason")
    if status != RUN_OK:
        return RUN_SKIPPED, (str(reason) if reason else "unspecified")
    return RUN_OK, None


# ── L1:KB schema_notes vs 活库 ────────────────────────────────────────


def from_schema_drift(report: dict[str, Any], datasource: str = "") -> DriftReport:
    """``memory.schema_drift.detect_drift`` 的报告 → :class:`DriftReport`。

    输入契约(已验证,``schema_drift.py``)::

        {"datasource", "new_tables": [t], "gone_tables": [t],
         "column_changes": {t: {"added": [c], "removed": [c]}},
         "status": "ok"|"skipped", "skip_reason": str|None}

    表名与列名在该模块内部已统一小写(:func:`_live_column_sets` /
    :func:`_kb_column_set` 同口径),此处仍过一遍
    :func:`normalize_subject` —— 规范化只能有一处实现。
    """
    ds = datasource or str(report.get("datasource") or "")
    status, skip_reason = _status_of(report)
    if status != RUN_OK:
        return DriftReport(
            datasource=ds, status=status, items=[], generated_at=_now(),
            skip_reason=skip_reason,
        )

    items: list[DriftItem] = []

    for t in report.get("new_tables") or []:
        subject = normalize_subject(t)
        if not subject:
            continue
        items.append(DriftItem(
            level=L1, kind="table_added", subject=subject,
            severity=SEVERITY_INFO,
            detail={"table": t, "note": "活库有、schema_notes.yml 未记录"},
        ))

    for t in report.get("gone_tables") or []:
        subject = normalize_subject(t)
        if not subject:
            continue
        items.append(DriftItem(
            level=L1, kind="table_removed", subject=subject,
            severity=SEVERITY_WARNING,
            detail={"table": t, "note": "schema_notes.yml 记录的表在活库已不存在"},
        ))

    for table, changes in (report.get("column_changes") or {}).items():
        changes = changes or {}
        for col in changes.get("added") or []:
            subject = normalize_subject(f"{table}.{col}")
            if not subject:
                continue
            items.append(DriftItem(
                level=L1, kind="column_added", subject=subject,
                severity=SEVERITY_INFO,
                detail={"table": table, "column": col},
            ))
        for col in changes.get("removed") or []:
            subject = normalize_subject(f"{table}.{col}")
            if not subject:
                continue
            items.append(DriftItem(
                level=L1, kind="column_removed", subject=subject,
                severity=SEVERITY_WARNING,
                detail={"table": table, "column": col},
            ))

    return DriftReport(
        datasource=ds, status=RUN_OK, items=items, generated_at=_now(),
    )


# ── L2:semantics.yml vs catalog 快照 ──────────────────────────────────


def from_semantic_drift(report: dict[str, Any], datasource: str = "") -> DriftReport:
    """``SemanticLayerProvider.drift()`` 的报告 → :class:`DriftReport`。

    输入契约(已验证,``provider._compute_drift``)::

        {"stale": bool, "gone_tables": [dataset_name],
         "missing_fields": {dataset: [field_name]},
         "missing_keys": {dataset: [key]},          # 是 dict,不是 list
         "relationship_breaks": [{"name", "detail"}],
         "status": "ok"|"skipped", "skip_reason": str|None}

    三处必须小心的细节:

    1. ``missing_fields`` 里放的是**语义字段名**(``f.name``),不是物理列
       名 —— 比对的是 ``f.expression``,记录的是 ``f.name``。两者可以不同,
       所以 subject 用字段名,而表达式写进 detail 备查。
    2. ``missing_keys`` 是 **dict** 不是 list(键为数据集名)。写成 list
       遍历会静默产出 0 条 —— 这正是"空报告冒充无漂移"的又一副面孔。
    3. ``gone_tables`` 里是**数据集名**,不是物理表名。subject 用数据集名:
      门禁的 coverage 判定发生在语义层,那里只有数据集名。
    """
    status, skip_reason = _status_of(report)
    if status != RUN_OK:
        return DriftReport(
            datasource=datasource, status=status, items=[], generated_at=_now(),
            skip_reason=skip_reason,
        )

    items: list[DriftItem] = []

    for dataset in report.get("gone_tables") or []:
        subject = normalize_subject(dataset)
        if not subject:
            continue
        items.append(DriftItem(
            level=L2, kind="dataset_table_missing", subject=subject,
            severity=SEVERITY_CRITICAL,
            detail={"dataset": dataset,
                    "note": "semantics.yml 声明的数据集在 catalog 快照中不存在"},
        ))

    for dataset, fields in (report.get("missing_fields") or {}).items():
        for field in fields or []:
            subject = normalize_subject(f"{dataset}.{field}")
            if not subject:
                continue
            items.append(DriftItem(
                level=L2, kind="field_column_missing", subject=subject,
                severity=SEVERITY_CRITICAL,
                detail={"dataset": dataset, "field": field,
                        "note": "字段表达式为裸列名且该列已不在活库"
                                "(复杂表达式不做判定,故不在此列)"},
            ))

    for dataset, keys in (report.get("missing_keys") or {}).items():
        for key in keys or []:
            subject = normalize_subject(f"{dataset}.{key}")
            if not subject:
                continue
            items.append(DriftItem(
                level=L2, kind="key_column_missing", subject=subject,
                # warning 而非 critical:键列缺失削弱去重/计数口径,但不会让
                # 编译产物直接引用不存在的对象 —— 与字段缺失是两个后果。
                severity=SEVERITY_WARNING,
                detail={"dataset": dataset, "key": key,
                        "note": "primary_key / unique_keys 声明的列已不在活库"},
            ))

    for brk in report.get("relationship_breaks") or []:
        name = str((brk or {}).get("name") or "")
        subject = normalize_subject(name)
        if not subject:
            continue
        items.append(DriftItem(
            level=L2, kind="relationship_broken", subject=subject,
            severity=SEVERITY_CRITICAL,
            # 原样保留检测器拼好的可读串("table X gone; a.b")—— 它已经
            # 是排障时最直接要看的东西,重新结构化反而丢信息。
            detail={"relationship": name, "problems": (brk or {}).get("detail", "")},
        ))

    return DriftReport(
        datasource=datasource, status=RUN_OK, items=items, generated_at=_now(),
    )


def collect(
    schema_report: dict[str, Any] | None,
    semantic_report: dict[str, Any] | None,
    datasource: str,
) -> DriftReport:
    """把两条路的报告合成**一份** :class:`DriftReport`。

    **合成的语义**:任一路 ``skipped`` → 整体 ``skipped``。

    这是本模块最重要的一条判断。有人会主张"L1 查成了就该出 L1 的结果",
    但门禁的输入是一份报告,而 ``skipped`` 的部分恰恰是**未知**;一份
    「一半已查、一半未知」的报告若标 ``ok``,调用方看到 ``ok`` 就以为两面
    都干净 —— 未知被洗成了通过。宁可整体降级为"未完成",让调用方去重试,
    也不产出半真报告。这与 ``verifiable-loop`` 里"CI 静默通过比红更危险"
    是同一条原则。
    """
    parts: list[tuple[str, DriftReport]] = []
    if schema_report is not None:
        parts.append((L1, from_schema_drift(schema_report, datasource)))
    if semantic_report is not None:
        parts.append((L2, from_semantic_drift(semantic_report, datasource)))

    if not parts:
        return DriftReport(
            datasource=datasource, status=RUN_SKIPPED, items=[],
            generated_at=_now(), skip_reason="no_detector_ran",
        )

    skipped = [p for _, p in parts if p.status != RUN_OK]
    if skipped:
        reasons = ",".join(sorted({p.skip_reason or "unspecified" for p in skipped}))
        # ``levels_verified`` 留空:未完成的检测什么都没验证。哪怕 L1 那一路
        # 跑完且干净,它的结论也没有 L2 兜底 —— 半份结论单独采信正是 I3 要挡的。
        return DriftReport(
            datasource=datasource, status=RUN_SKIPPED, items=[],
            generated_at=_now(), skip_reason=reasons,
        )

    # verified = 「这一级的检测器**真的跑了并给出了结论**」。没有语义层的
    # datasource 其 semantic_report 为 None(L2 那路压根没进 parts)——
    # 于是 L2 不在 verified 里,门禁不会把「没有契约可违反」误当成「契约成立」。
    return DriftReport(
        datasource=datasource, status=RUN_OK,
        items=[i for _, p in parts for i in p.items],
        generated_at=_now(),
        levels_verified=frozenset(lv for lv, _ in parts),
    )
