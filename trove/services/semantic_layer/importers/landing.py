"""导入落点:IR → OSSIE 文档 / 有序草稿 payload,并走既有写入门。

三条铁律:
- **preview 零写入**:只读现有文件,产出报告;
- **bootstrap 走 ``KbService.init_semantics``**(I1 白名单豁免,与 ``/kb init``
  同路径),写前做回环计数比对 + 阻断 lint;
- **stage 走 ``ChangeService.open``**,只写 ``semantic_changes.yml`` +
  ``.staging/``,``semantics.yml`` 一个字节都不动。
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from trove.services.kb.lint import lint_semantics_document
from trove.services.semantic_layer.importers.base import (
    ImportedSemantic,
    SemanticImportError,
)
from trove.services.semantic_layer.issues import blocking_issues, structured_issues
from trove.services.semantic_layer.manage import _apply_draft
from trove.services.semantic_layer.ossie import (
    document_refusal,
    is_legacy_document,
    parse_ossie,
    stamp_ossie_version,
)

_ANSI = "ANSI_SQL"
_DUMP_KWARGS = dict(default_flow_style=False, allow_unicode=True, sort_keys=False)


# ── IR → 目标形状 ───────────────────────────────────────────


def _expression(value: str) -> dict[str, Any]:
    return {"dialects": [{"dialect": _ANSI, "expression": value}]}


def build_document(imported: ImportedSemantic, *, model_name: str) -> dict[str, Any]:
    """IR → 完整 OSSIE 文档(bootstrap 用;字段嵌在 dataset 里)。"""
    datasets: list[dict[str, Any]] = []
    for ds in imported.datasets:
        entry: dict[str, Any] = {"name": ds.name, "source": ds.source}
        if ds.primary_key:
            entry["primary_key"] = list(ds.primary_key)
        if ds.unique_keys:
            entry["unique_keys"] = [list(k) for k in ds.unique_keys]
        if ds.description:
            entry["description"] = ds.description
        fields: list[dict[str, Any]] = []
        for f in ds.fields:
            field: dict[str, Any] = {
                "name": f.name,
                "expression": _expression(f.expression),
            }
            if f.datatype:
                field["datatype"] = f.datatype
            if f.description:
                field["description"] = f.description
            if f.is_time:
                field["dimension"] = {"is_time": True}
            fields.append(field)
        if fields:
            entry["fields"] = fields
        datasets.append(entry)

    metrics: list[dict[str, Any]] = []
    for m in imported.metrics:
        metric: dict[str, Any] = {
            "name": m.name,
            "expression": _expression(m.expression),
        }
        if m.description:
            metric["description"] = m.description
        if m.metric_type:
            metric["type"] = m.metric_type
        metrics.append(metric)

    relationships: list[dict[str, Any]] = []
    for r in imported.relationships:
        relationships.append({
            "name": r.name,
            "from": r.from_dataset,
            "to": r.to_dataset,
            "from_columns": list(r.from_columns),
            "to_columns": list(r.to_columns),
            "cardinality": r.cardinality,
        })

    model: dict[str, Any] = {"name": model_name, "datasets": datasets}
    if metrics:
        model["metrics"] = metrics
    if relationships:
        model["relationships"] = relationships
    return stamp_ossie_version(model)


def build_payloads(imported: ImportedSemantic) -> list[dict[str, Any]]:
    """IR → 有序草稿 payload(dataset → field → metric → relationship)。"""
    payloads: list[dict[str, Any]] = []
    for ds in imported.datasets:
        payload: dict[str, Any] = {"source": ds.source}
        if ds.primary_key:
            payload["primary_key"] = list(ds.primary_key)
        if ds.unique_keys:
            payload["unique_keys"] = [list(k) for k in ds.unique_keys]
        if ds.description:
            payload["description"] = ds.description
        payloads.append({
            "kind": "dataset", "action": "upsert", "name": ds.name,
            "payload": payload,
        })
    for ds in imported.datasets:
        for f in ds.fields:
            payload = {"expression": f.expression}
            if f.datatype:
                payload["datatype"] = f.datatype
            if f.description:
                payload["description"] = f.description
            if f.is_time:
                payload["is_time"] = True
            payloads.append({
                "kind": "field", "action": "upsert",
                "name": f"{ds.name}.{f.name}", "payload": payload,
            })
    for m in imported.metrics:
        payload = {
            "expression": m.expression,
            "datasets": list(m.datasets),
        }
        if m.description:
            payload["definition"] = m.description
        if m.metric_type:
            payload["type"] = m.metric_type
        payloads.append({
            "kind": "metric", "action": "upsert", "name": m.name,
            "payload": payload,
        })
    for r in imported.relationships:
        payloads.append({
            "kind": "relationship", "action": "upsert", "name": r.name,
            "payload": {
                "from": r.from_dataset,
                "to": r.to_dataset,
                "from_columns": list(r.from_columns),
                "to_columns": list(r.to_columns),
                "cardinality": r.cardinality,
            },
        })
    return payloads


# ── 读现有文件 / 预览 ───────────────────────────────────────


def load_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def has_model(doc: dict[str, Any]) -> bool:
    """现有文档里有没有可投影的模型(0.2 扁平根;legacy 也算占用)。"""
    if not isinstance(doc, dict):
        return False
    if is_legacy_document(doc):
        models = doc.get("semantic_model") or []
        model = models[0] if models and isinstance(models[0], dict) else {}
    else:
        model = doc
    return bool(model.get("datasets") or model.get("metrics") or model.get("relationships"))


def preview_report(
    imported: ImportedSemantic,
    existing: dict[str, Any],
    *,
    mode: str,
    datasource: str,
    dialect: str,
) -> dict[str, Any]:
    """零写入报告:目标文档 / 投影后的 lint / 落点建议。"""
    occupied = has_model(existing)
    blocking: list[str] = []
    refusal = document_refusal(existing)
    if refusal:
        # 现有文件不可解释(legacy / 缺版本 / 更新版本):不往它上面投影,
        # 只报阻断,让管理员先迁移/升级。
        blocking.append(f"现有 semantics.yml 不可读: {refusal}")
        projected = build_document(imported, model_name=datasource)
    elif occupied:
        projected = copy.deepcopy(existing)
        for payload in build_payloads(imported):
            fake = {
                "id": "import-preview",
                "kind": payload["kind"],
                "action": payload["action"],
                "name": payload["name"],
                "payload": payload["payload"],
            }
            try:
                _apply_draft(projected, fake, dialect)
            except (ValueError, TypeError) as exc:
                blocking.append(f"投影失败({payload['kind']} {payload['name']}): {exc}")
    else:
        projected = build_document(imported, model_name=datasource)
    issues = lint_semantics_document(projected, dialect=dialect)
    blocking.extend(blocking_issues(issues))
    # 去重保序
    blocking = list(dict.fromkeys(blocking))
    return imported.to_report(
        mode=mode,
        datasource=datasource,
        suggestion="stage" if occupied else "bootstrap",
        blocking=blocking,
        issue_items=structured_issues(issues),
        landed=None,
    )


# ── bootstrap ───────────────────────────────────────────────


async def land_bootstrap(
    kb: Any,
    datasource: str,
    imported: ImportedSemantic,
    *,
    force: bool = False,
    dialect: str = "mysql",
) -> dict[str, Any]:
    path = kb.semantics_path(datasource)
    existing = load_document(path)
    if is_legacy_document(existing) and not force:
        raise SemanticImportError(
            f"数据源 {datasource} 的 semantics.yml 仍是旧版 v0.1 数组包装 —— "
            "请先迁移(scripts/migrate_semantics_v02.py),或显式 --force 整体覆盖")
    if has_model(existing) and not force:
        raise SemanticImportError(
            f"数据源 {datasource} 已有非空语义模型 —— bootstrap 会整体覆盖。"
            "改用 --mode stage 走审批流;确需覆盖请显式加 --force")

    doc = build_document(imported, model_name=datasource)
    text = yaml.safe_dump(doc, **_DUMP_KWARGS)
    parsed = parse_ossie(text, preferred_dialect=dialect)
    roundtrip = {
        "datasets": len(parsed.datasets),
        "fields": sum(len(d.fields) for d in parsed.datasets),
        "metrics": len(parsed.metrics),
        "relationships": len(parsed.relationships),
    }
    expected = imported.counts()
    if roundtrip != expected:
        raise SemanticImportError(
            "导入文档回环计数不一致,已中止(写进去的会与看到的不是同一份模型): "
            f"期望 {expected},解析回 {roundtrip}",
            blocking=[f"roundtrip mismatch: expected={expected} parsed={roundtrip}"])

    issues = lint_semantics_document(doc, dialect=dialect)
    blocking = blocking_issues(issues)
    if blocking:
        raise SemanticImportError(
            "导入结果未过阻断 lint,已中止(未写盘): " + "; ".join(blocking),
            blocking=blocking)

    written = kb.init_semantics(doc, datasource, overwrite=force, force=force)
    if not written:
        raise SemanticImportError(
            f"init_semantics 拒绝写入 {datasource}/semantics.yml"
            "(已有文件且未加 --force,或磁盘资产版本门拒绝)")
    await kb.force_sync(datasource)
    commit = await kb.git_commit(
        datasource,
        f"semantic: import {imported.source} ({imported.counts()})",
        files=["semantics.yml"],
        trailers={"Generator": "semantic.import", "Source": imported.source},
    )
    return {
        "mode": "bootstrap",
        "path": str(path),
        "written": True,
        "overwrote": force,
        "git": commit,
    }


# ── stage ───────────────────────────────────────────────────


def _fingerprint(payloads: list[dict[str, Any]]) -> str:
    blob = json.dumps(payloads, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


async def land_stage(
    kb: Any,
    datasource: str,
    imported: ImportedSemantic,
    *,
    note: str = "",
    dialect: str = "mysql",
) -> dict[str, Any]:
    existing = load_document(kb.semantics_path(datasource))
    if is_legacy_document(existing):
        raise SemanticImportError(
            f"数据源 {datasource} 的 semantics.yml 仍是旧版 v0.1 数组包装 —— "
            "请先迁移(scripts/migrate_semantics_v02.py),再走 stage 增量审批")
    if not has_model(existing):
        raise SemanticImportError(
            f"数据源 {datasource} 还没有语义模型 —— stage 是增量审批,"
            "空模型请用 --mode bootstrap")

    payloads = build_payloads(imported)
    if not payloads:
        raise SemanticImportError("没有可导入的声明(解析结果为空)")

    from trove.services.semantic_layer.changes import ChangeError, ChangeService

    changes = ChangeService(kb)
    fp = _fingerprint(payloads)
    marker = f"[fp:{fp}]"
    for rec in changes.list(datasource, status="open"):
        if marker in (rec.note or ""):
            return {
                "mode": "stage",
                "path": str(changes.changes_path(datasource)),
                "written": False,
                "duplicate": True,
                "change_id": rec.id,
                "status": rec.status,
            }
    note_text = (
        f"{note} " if note.strip() else ""
    ) + f"semantic import ({imported.source}): {imported.input_path} {marker}"
    try:
        record = await changes.open(
            datasource,
            origin="manual",
            payloads=payloads,
            note=note_text,
            dialect=dialect,
        )
    except ChangeError as exc:
        raise SemanticImportError(f"开单失败: {exc}") from exc
    return {
        "mode": "stage",
        "path": str(changes.changes_path(datasource)),
        "written": True,
        "duplicate": False,
        "change_id": record.get("id", ""),
        "status": record.get("status", "open"),
    }
