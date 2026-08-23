"""SemanticManager — 语义层管理服务（admin UI + 审批流）。

单一真源 = 数据源的 KB ``semantics.yml``（OSSIE ``semantic_model``，kb init
生成 + 人审）。读侧:``parse_ossie`` → SemanticModel、``lint_semantics`` →
issue 列表。写侧走 **semantic_drafts.yml** 的两层审批:

    pending（草稿）→ confirm（应用到 semantics.yml + 标记 applied）
                     → reject（标记 rejected,丢弃）

confirm 用 dict 级原地改保留手写内容（多方言条目、ai_context.instructions、
额外声明）,随后 ``force_sync`` 刷新 SQLite 镜像。表达式在 confirm 时做
SQLGlot 校验,坏条目拒绝写入。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlglot import ErrorLevel, exp, parse_one

from trove.services.datasource.naming import is_path_safe
from trove.services.kb.lint import lint_semantics
from trove.services.kb.service import KbService
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)
from trove.services.semantic_layer.ossie import parse_ossie

logger = logging.getLogger(__name__)

_ANSI = "ANSI_SQL"
_KINDS = {"metric", "field", "dataset"}
_ACTIONS = {"upsert", "delete"}

_DUMP_KWARGS = dict(
    default_flow_style=False, allow_unicode=True, sort_keys=False,
)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _dump_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, **_DUMP_KWARGS), encoding="utf-8",
    )


def _check_expr(expr: str, dialect: str | None, label: str) -> None:
    """SQLGlot 严格解析:坏表达式在 confirm 阶段拒绝,不落盘。

    宽松解析会把 ``SELEC broken`` 解成 ``Alias``(顶层 Alias 不是合法
    表达式),与 lint 的判定保持一致;真正的聚合/列表达式顶层不会是 Alias。
    """
    read = dialect or "sqlite"
    try:
        tree = parse_one(expr, read=read, error_level=ErrorLevel.RAISE)
    except Exception as e:
        raise ValueError(f"{label} 表达式无法解析: {e}") from e
    if isinstance(tree, exp.Alias):
        raise ValueError(f"{label} 表达式无法解析(语法错误)")


def _clean_synonyms(raw: Any) -> list[str]:
    return [s for s in (raw or []) if s and str(s).strip()]


# ── 序列化(管理页展示用,与模型 dataclass 一一对应) ────────


def _field_to_dict(f: SemanticField) -> dict[str, Any]:
    return {
        "name": f.name,
        "expression": f.expression,
        "datatype": f.datatype,
        "is_time": f.is_time,
        "description": f.description,
        "synonyms": list(f.synonyms),
        "semantic_role": f.semantic_role,
        "enum_display": dict(f.enum_display),
    }


def _dataset_to_dict(d: SemanticDataset) -> dict[str, Any]:
    return {
        "name": d.name,
        "source": d.source,
        "primary_key": list(d.primary_key),
        "description": d.description,
        "synonyms": list(d.synonyms),
        "fields": [_field_to_dict(f) for f in d.fields],
    }


def _metric_to_dict(m: SemanticMetric) -> dict[str, Any]:
    return {
        "name": m.name,
        "expression": m.expression,
        "synonyms": list(m.synonyms),
        "datasets": list(m.datasets),
        "definition": m.definition,
    }


def _relationship_to_dict(r: SemanticRelationship) -> dict[str, Any]:
    return {
        "name": r.name,
        "from": r.from_,
        "to": r.to,
        "from_columns": list(r.from_columns),
        "to_columns": list(r.to_columns),
        "cardinality": r.cardinality,
    }


def _model_to_dict(m: SemanticModel) -> dict[str, Any]:
    return {
        "name": m.name,
        "description": m.description,
        "instructions": m.instructions,
        "metrics": [_metric_to_dict(x) for x in m.metrics],
        "datasets": [_dataset_to_dict(x) for x in m.datasets],
        "relationships": [_relationship_to_dict(x) for x in m.relationships],
    }


# ── 文档层应用(dict 级原地改,保留手写内容) ────────────────


def _model_of(data: dict[str, Any]) -> dict[str, Any]:
    """semantic_model[0] 的原地引用;缺省时创建最小模型。"""
    models = data.setdefault("semantic_model", [])
    if models and isinstance(models[0], dict):
        return models[0]
    model: dict[str, Any] = {"name": "", "datasets": [], "metrics": []}
    models.append(model)
    return model


def _metric_payload_to_ossie(name: str, payload: dict[str, Any], dialect: str | None) -> dict[str, Any]:
    expr = str(payload.get("expression") or "").strip()
    if not expr:
        raise ValueError("metric 表达式必填")
    _check_expr(expr, dialect, f"指标「{name}」")
    metric: dict[str, Any] = {
        "name": name,
        "expression": {"dialects": [{"dialect": _ANSI, "expression": expr}]},
    }
    syns = _clean_synonyms(payload.get("synonyms"))
    if syns:
        metric["ai_context"] = {"synonyms": syns}
    if payload.get("definition"):
        metric["description"] = str(payload["definition"])
    return metric


def _apply_metric(model: dict[str, Any], action: str, name: str,
                  payload: dict[str, Any] | None, dialect: str | None) -> None:
    metrics = model.setdefault("metrics", [])
    if action == "delete":
        model["metrics"] = [m for m in metrics if m.get("name") != name]
        return
    metric = _metric_payload_to_ossie(name, payload or {}, dialect)
    declared = {d.get("name") for d in model.get("datasets", []) if d.get("name")}
    for t in payload.get("datasets") or []:
        if t and t not in declared:
            model.setdefault("datasets", []).append({"name": t})
            declared.add(t)
    idx = next((i for i, m in enumerate(model["metrics"]) if m.get("name") == name), None)
    if idx is not None:
        model["metrics"][idx] = metric
    else:
        model["metrics"].append(metric)


def _apply_field(model: dict[str, Any], action: str, name: str,
                 payload: dict[str, Any] | None, dialect: str | None) -> None:
    dataset_name, sep, field_name = name.partition(".")
    if not sep or not dataset_name or not field_name:
        raise ValueError("字段目标必须是 dataset.field 形式")
    ds = next((d for d in model.get("datasets", []) if d.get("name") == dataset_name), None)
    if ds is None:
        raise ValueError(f"数据集不存在: {dataset_name}")
    fields = ds.setdefault("fields", [])
    if action == "delete":
        ds["fields"] = [f for f in fields if f.get("name") != field_name]
        return
    expr = str((payload or {}).get("expression") or "").strip()
    if not expr:
        raise ValueError("字段表达式必填")
    _check_expr(expr, dialect, f"字段「{name}」")
    field: dict[str, Any] = {
        "name": field_name,
        "expression": {"dialects": [{"dialect": _ANSI, "expression": expr}]},
    }
    if payload.get("datatype"):
        field["datatype"] = str(payload["datatype"])
    if payload.get("semantic_role"):
        field["semantic_role"] = str(payload["semantic_role"])
    syns = _clean_synonyms(payload.get("synonyms"))
    if syns:
        field["ai_context"] = {"synonyms": syns}
    if payload.get("description"):
        field["description"] = str(payload["description"])
    if payload.get("is_time") is not None:
        field["dimension"] = {"is_time": bool(payload["is_time"])}
    display = payload.get("enum_display")
    if isinstance(display, dict) and display:
        field["enum_display"] = {str(k): str(v) for k, v in display.items()}
    idx = next((i for i, f in enumerate(ds["fields"]) if f.get("name") == field_name), None)
    if idx is not None:
        ds["fields"][idx] = field
    else:
        ds["fields"].append(field)


def _apply_dataset(model: dict[str, Any], action: str, name: str,
                   payload: dict[str, Any] | None, dialect: str | None) -> None:
    datasets = model.setdefault("datasets", [])
    if action == "delete":
        model["datasets"] = [d for d in datasets if d.get("name") != name]
        return
    payload = payload or {}
    ds: dict[str, Any] = {
        "name": name,
        "source": str(payload.get("source") or name),
        "primary_key": [str(pk) for pk in (payload.get("primary_key") or [])],
    }
    syns = _clean_synonyms(payload.get("synonyms"))
    if syns:
        ds["ai_context"] = {"synonyms": syns}
    if payload.get("description"):
        ds["description"] = str(payload["description"])
    idx = next((i for i, d in enumerate(model["datasets"]) if d.get("name") == name), None)
    if idx is not None:
        old = model["datasets"][idx]
        # 仅改元数据时保留既有 fields(不在 payload 里重复声明)
        if not payload.get("fields") and old.get("fields"):
            ds["fields"] = old["fields"]
        model["datasets"][idx] = ds
    else:
        model["datasets"].append(ds)


def _apply_draft(data: dict[str, Any], draft: dict[str, Any], dialect: str | None) -> None:
    model = _model_of(data)
    kind = draft["kind"]
    action = draft["action"]
    name = draft["name"]
    payload = draft.get("payload")
    if kind == "metric":
        _apply_metric(model, action, name, payload, dialect)
    elif kind == "field":
        _apply_field(model, action, name, payload, dialect)
    elif kind == "dataset":
        _apply_dataset(model, action, name, payload, dialect)
    else:
        raise ValueError(f"未知草稿类型: {kind}")


class SemanticManager:
    """Per-datasource semantic layer management (reads + draft approval)."""

    def __init__(self, kb: KbService) -> None:
        self._kb = kb

    @property
    def kb_dir(self) -> Path:
        return self._kb.kb_dir

    def _semantics_path(self, datasource: str) -> Path:
        return self._kb.semantics_path(datasource)

    def _drafts_path(self, datasource: str) -> Path:
        return self.kb_dir / datasource / "semantic_drafts.yml"

    def _check_datasource(self, datasource: str) -> None:
        if not is_path_safe(datasource):
            raise ValueError(f"unsafe KB datasource name {datasource!r}")

    # ── 读 ────────────────────────────────────────────────

    def enabled(self, datasource: str) -> bool:
        return self._semantics_path(datasource).exists()

    def model(self, datasource: str, dialect: str | None = None) -> SemanticModel | None:
        """解析后的语义模型;文件缺失/坏 → None(绝不抛进问题流)。"""
        path = self._semantics_path(datasource)
        if not path.exists():
            return None
        try:
            return parse_ossie(path.read_text(encoding="utf-8"), preferred_dialect=dialect or "sqlite")
        except Exception as e:
            logger.warning("semantic model parse failed (%s): %s", datasource, e)
            return None

    def issues(self, datasource: str, dialect: str | None = None) -> list[str]:
        """lint_semantics 逐模型输出:重复定义/坏表达式/非法关系。"""
        path = self._semantics_path(datasource)
        if not path.exists():
            return []
        data = _load_yaml(path)
        if not data:
            return ["semantics.yml 无法解析(YAML 语法错误)"]
        issues: list[str] = []
        for entry in data.get("semantic_model", []) or []:
            if isinstance(entry, dict):
                issues += lint_semantics(entry, dialect=dialect or "sqlite")
        return issues

    def drafts(self, datasource: str) -> dict[str, list[dict[str, Any]]]:
        data = _load_yaml(self._drafts_path(datasource))
        entries = data.get("drafts", []) if isinstance(data, dict) else []
        out: dict[str, list[dict[str, Any]]] = {"pending": [], "applied": [], "rejected": []}
        for e in entries:
            status = e.get("status", "pending")
            if status in out:
                out[status].append(e)
            else:
                out["pending"].append(e)
        return out

    async def detail(self, datasource: str, dialect: str | None = None) -> dict[str, Any]:
        model = self.model(datasource, dialect)
        return {
            "enabled": self.enabled(datasource),
            "model": _model_to_dict(model) if model is not None else None,
            "issues": self.issues(datasource, dialect),
            "drafts": self.drafts(datasource),
        }

    # ── 审批流写 ──────────────────────────────────────────

    async def create_draft(
        self, datasource: str, kind: str, action: str, name: str,
        payload: dict[str, Any] | None = None, note: str = "",
    ) -> dict[str, Any]:
        """建 pending 草稿(semantic_drafts.yml)。不碰 semantics.yml。"""
        self._check_datasource(datasource)
        if kind not in _KINDS:
            raise ValueError(f"kind 必须为 {sorted(_KINDS)} 之一")
        if action not in _ACTIONS:
            raise ValueError(f"action 必须为 {sorted(_ACTIONS)} 之一")
        if not name:
            raise ValueError("name 必填")
        if action == "upsert" and not payload:
            raise ValueError("upsert 草稿需要 payload")
        entry: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "action": action,
            "name": name,
            "payload": payload or None,
            "note": note or "",
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        path = self._drafts_path(datasource)
        data = _load_yaml(path)
        drafts = list(data.get("drafts", []) if isinstance(data, dict) else [])
        drafts.append(entry)
        data["drafts"] = drafts
        _dump_yaml(path, data)
        await self._kb.force_sync(datasource)
        return dict(entry)

    def _find_draft(self, datasource: str, draft_id: str) -> tuple[dict[str, Any], Path]:
        path = self._drafts_path(datasource)
        data = _load_yaml(path)
        drafts = list(data.get("drafts", []) if isinstance(data, dict) else [])
        draft = next((d for d in drafts if d.get("id") == draft_id), None)
        if draft is None:
            raise KeyError(f"草稿不存在: {draft_id}")
        return draft, path

    def _save_drafts(self, path: Path, drafts: list[dict[str, Any]]) -> None:
        _dump_yaml(path, {"drafts": drafts})

    async def confirm_draft(
        self, datasource: str, draft_id: str, dialect: str | None = None,
    ) -> dict[str, Any]:
        """审批通过:应用到 semantics.yml → 标记 applied → 刷新镜像。"""
        self._check_datasource(datasource)
        draft, path = self._find_draft(datasource, draft_id)
        if draft.get("status") != "pending":
            raise ValueError(f"草稿 {draft_id} 已 {draft.get('status')}")
        semantics = self._semantics_path(datasource)
        data = _load_yaml(semantics) if semantics.exists() else {}
        try:
            _apply_draft(data, draft, dialect)
        except ValueError as e:
            raise ValueError(f"草稿确认失败: {e}") from e
        _dump_yaml(semantics, data)
        draft["status"] = "applied"
        drafts = self._drafts_with(datasource, draft)
        self._save_drafts(path, drafts)
        await self._kb.force_sync(datasource)
        return dict(draft)

    async def reject_draft(self, datasource: str, draft_id: str) -> dict[str, Any]:
        """驳回:仅标记 rejected,不改 semantics.yml。"""
        self._check_datasource(datasource)
        draft, path = self._find_draft(datasource, draft_id)
        if draft.get("status") != "pending":
            raise ValueError(f"草稿 {draft_id} 已 {draft.get('status')}")
        draft["status"] = "rejected"
        drafts = self._drafts_with(datasource, draft)
        self._save_drafts(path, drafts)
        await self._kb.force_sync(datasource)
        return dict(draft)

    def _drafts_with(self, datasource: str, updated: dict[str, Any]) -> list[dict[str, Any]]:
        data = _load_yaml(self._drafts_path(datasource))
        drafts = list(data.get("drafts", []) if isinstance(data, dict) else [])
        return [updated if d.get("id") == updated["id"] else d for d in drafts]
