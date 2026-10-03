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

import copy
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlglot import ErrorLevel, exp, parse_one

from trove.services.datasource.naming import is_path_safe
from trove.services.kb.lint import lint_semantics_document
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
_KINDS = {"metric", "field", "dataset", "topic"}
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


def _reject_bad_semantics(data: dict[str, Any], dialect: str | None) -> None:
    """写盘前门禁:坏语义拒绝持久化,而不只是拒绝进 git 审计历史。

    git 的 pre-commit lint 只能保证坏语义不落进 commit —— 那时文件已
    ``_dump_yaml`` 写盘、``force_sync`` 进了运行时检索。这里把同一套
    ``lint_semantics`` 前移到写盘之前:issues 非空即 ``raise``,不落盘、
    不刷新镜像,坏语义永不进入运行时。git 侧门禁保留作第二道兜底。
    """
    issues = lint_semantics_document(data, dialect=dialect or "sqlite")
    if issues:
        raise ValueError("语义校验未通过,拒绝写入: " + "; ".join(issues))


def _clean_synonyms(raw: Any) -> list[str]:
    return [s for s in (raw or []) if s and str(s).strip()]


def _carryover(old: dict[str, Any] | None, new: dict[str, Any], *keys: str) -> None:
    """实体替换时保留 payload 未覆盖的手写字段(custom_extensions / label /
    unique_keys / examples 等 OSSIE 扩展面不被 upsert 抹掉)。"""
    if not old:
        return
    for key in keys:
        if key in old and (key not in new or new.get(key) in (None, "", [], {})):
            new[key] = old[key]


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
        "value_aliases": {k: list(v) for k, v in f.value_aliases.items()},
        "label": f.label,
        "examples": list(f.examples),
        "custom_extensions": list(f.custom_extensions),
        "mask": f.mask,
    }


def _dataset_to_dict(d: SemanticDataset) -> dict[str, Any]:
    return {
        "name": d.name,
        "source": d.source,
        "primary_key": list(d.primary_key),
        "unique_keys": [list(k) for k in d.unique_keys],
        "row_filter": d.row_filter,
        "description": d.description,
        "synonyms": list(d.synonyms),
        "fields": [_field_to_dict(f) for f in d.fields],
        "examples": list(d.examples),
        "custom_extensions": list(d.custom_extensions),
    }


def _metric_to_dict(m: SemanticMetric) -> dict[str, Any]:
    return {
        "name": m.name,
        "expression": m.expression,
        "synonyms": list(m.synonyms),
        "datasets": list(m.datasets),
        "definition": m.definition,
        "metric_type": m.metric_type,
        "filter": m.filter,
        "agg_time_dimension": m.agg_time_dimension,
        "non_additive": m.non_additive,
        "datatype": m.datatype,
        "examples": list(m.examples),
        "custom_extensions": list(m.custom_extensions),
    }


def _relationship_to_dict(r: SemanticRelationship) -> dict[str, Any]:
    return {
        "name": r.name,
        "from": r.from_,
        "to": r.to,
        "from_columns": list(r.from_columns),
        "to_columns": list(r.to_columns),
        "cardinality": r.cardinality,
        "fan_out": r.fan_out,
        "examples": list(r.examples),
        "custom_extensions": list(r.custom_extensions),
    }


def _model_to_dict(m: SemanticModel) -> dict[str, Any]:
    spine = None
    if m.time_spine is not None:
        spine = {
            "field": m.time_spine.field,
            "granularity": m.time_spine.granularity,
            "fill": m.time_spine.fill,
        }
    return {
        "name": m.name,
        "description": m.description,
        "instructions": m.instructions,
        "metrics": [_metric_to_dict(x) for x in m.metrics],
        "datasets": [_dataset_to_dict(x) for x in m.datasets],
        "relationships": [_relationship_to_dict(x) for x in m.relationships],
        "version": m.version,
        "examples": list(m.examples),
        "custom_extensions": list(m.custom_extensions),
        "time_spine": spine,
        # 主题域(可选段):管理端看得见声明;作用域的实际生效口径在
        # ``semantic_layer/topics.resolve_topic``(声明 ∩ 模型当前数据集)。
        "topics": [
            {
                "name": t.name,
                "description": t.description,
                "synonyms": list(t.synonyms),
                "datasets": list(t.datasets),
                "metrics": list(t.metrics),
                "examples": list(t.examples),
                "custom_extensions": list(t.custom_extensions),
            }
            for t in m.topics
        ],
        # 形状即 OSSIE 形状(无 dialects 嵌套)→ 管理页看得见、可原样回灌
        "masking": {
            "default_policy": m.masking.default_policy,
            "bypass_scopes": list(m.masking.bypass_scopes),
            "hash_salt_ref": m.masking.hash_salt_ref,
        },
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
    # 派生/比率度量(表达式引用其他 metric 名):OSSIE type 字段
    if payload.get("type"):
        metric["type"] = str(payload["type"])
    # OSSIE v0.2.0.dev0 扩展面:datatype / examples / custom_extensions
    if payload.get("datatype"):
        metric["datatype"] = str(payload["datatype"])
    examples = [str(e) for e in (payload.get("examples") or []) if str(e).strip()]
    if examples:
        ai = metric.setdefault("ai_context", {})
        ai["examples"] = examples
    ext = [e for e in (payload.get("custom_extensions") or [])
           if isinstance(e, dict) and e.get("vendor_name")]
    if ext:
        metric["custom_extensions"] = ext
    return metric


def _apply_metric(model: dict[str, Any], action: str, name: str,
                  payload: dict[str, Any] | None, dialect: str | None) -> None:
    metrics = model.setdefault("metrics", [])
    if action == "delete":
        model["metrics"] = [m for m in metrics if m.get("name") != name]
        return
    metric = _metric_payload_to_ossie(name, payload or {}, dialect)
    declared = {d.get("name") for d in model.get("datasets", []) if d.get("name")}
    # 锚定的数据集必须已声明:**显式报错**,不再静默补建空壳条目。补出来的
    # stub 无 source 无字段,作者从没见过它,编译器却会把指标锚到一个查询期
    # 必然 MISS 的空数据集上 —— 静默补建把「写错了名字」变成「运行期才发现」。
    undeclared = list(dict.fromkeys(
        str(t) for t in (payload.get("datasets") or []) if t and str(t) not in declared))
    if undeclared:
        raise ValueError(
            f"指标「{name}」锚定的数据集未声明: {', '.join(undeclared)}"
            "(修正数据集名,或先在 datasets 里显式声明该数据集)")
    idx = next((i for i, m in enumerate(model["metrics"]) if m.get("name") == name), None)
    if idx is not None:
        old = model["metrics"][idx]
        _carryover(old, metric, "filter", "agg_time_dimension", "non_additive",
                   "datatype", "examples", "custom_extensions", "ai_context")
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
    # 值语义字典(多标签):value_aliases → {code: [别名]}(兼容 list/逗号串)。
    val_aliases = payload.get("value_aliases") or {}
    if isinstance(val_aliases, dict) and val_aliases:
        field.setdefault("ai_context", {})["value_aliases"] = {
            str(code): (
                list(labels) if isinstance(labels, (list, tuple))
                else [str(ln).strip() for ln in str(labels).split(",") if str(ln).strip()]
            )
            for code, labels in val_aliases.items()
        }
    # OSSIE v0.2.0.dev0 扩展面:label / examples / custom_extensions
    if payload.get("label"):
        field["label"] = str(payload["label"])
    examples = [str(e) for e in (payload.get("examples") or []) if str(e).strip()]
    if examples:
        ai = field.setdefault("ai_context", {})
        ai["examples"] = examples
    ext = [e for e in (payload.get("custom_extensions") or [])
           if isinstance(e, dict) and e.get("vendor_name")]
    if ext:
        field["custom_extensions"] = ext
    idx = next((i for i, f in enumerate(ds["fields"]) if f.get("name") == field_name), None)
    if idx is not None:
        old = ds["fields"][idx]
        _carryover(old, field, "datatype", "semantic_role", "enum_display",
                   "value_aliases", "label", "examples", "custom_extensions",
                   "ai_context", "dimension", "description")
        # value_aliases 存在旧 ai_context 里(嵌套),新字段未带时从旧合并,
        # 避免只改同义词的 upsert 丢值词典。
        if isinstance(old.get("ai_context"), dict):
            old_va = old["ai_context"].get("value_aliases")
            new_ai = field.setdefault("ai_context", {})
            if old_va and "value_aliases" not in new_ai:
                new_ai["value_aliases"] = old_va
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
    # OSSIE v0.2.0.dev0 扩展面:unique_keys / examples / custom_extensions
    uks = [[str(k) for k in keys]
           for keys in (payload.get("unique_keys") or [])
           if isinstance(keys, list) and keys]
    if uks:
        ds["unique_keys"] = uks
    if payload.get("row_filter"):
        ds["row_filter"] = str(payload["row_filter"]).strip()
    examples = [str(e) for e in (payload.get("examples") or []) if str(e).strip()]
    if examples:
        ai = ds.setdefault("ai_context", {})
        ai["examples"] = examples
    ext = [e for e in (payload.get("custom_extensions") or [])
           if isinstance(e, dict) and e.get("vendor_name")]
    if ext:
        ds["custom_extensions"] = ext
    idx = next((i for i, d in enumerate(model["datasets"]) if d.get("name") == name), None)
    if idx is not None:
        old = model["datasets"][idx]
        # 仅改元数据时保留既有 fields(不在 payload 里重复声明)
        if not payload.get("fields") and old.get("fields"):
            ds["fields"] = old["fields"]
        _carryover(old, ds, "unique_keys", "row_filter", "examples",
                   "custom_extensions", "ai_context", "description", "fields")
        model["datasets"][idx] = ds
    else:
        model["datasets"].append(ds)


def _apply_topic(model: dict[str, Any], action: str, name: str,
                 payload: dict[str, Any] | None, dialect: str | None) -> None:
    """主题域草稿应用(语义收敛边界,与 metric/field/dataset 同一审批流)。

    两条硬校验,都在**写盘前**:

    * ``datasets`` 必填且每个名字都必须在模型里已声明 —— 主题域的作用域是
      ``声明 ∩ 模型``(见 ``semantic_layer/topics.resolve_topic``),声明一个
      不存在的名字只会让作用域静默变窄;这里显式报错,把「写错了名字」挡在
      草稿确认时,而不是留给运行期去猜;
    * ``metrics`` 若声明,同样按名字引用既有指标(不在这里校验锚定是否落在
      域内 —— 那是文档 lint ``_lint_topics`` 的事,写盘门禁会跑同一份)。
    """
    topics = model.setdefault("topics", [])
    if action == "delete":
        model["topics"] = [t for t in topics if t.get("name") != name]
        return
    payload = payload or {}
    declared = {d.get("name") for d in model.get("datasets", []) if d.get("name")}
    datasets = [str(d) for d in (payload.get("datasets") or []) if str(d).strip()]
    undeclared = [d for d in dict.fromkeys(datasets) if d not in declared]
    if undeclared:
        raise ValueError(
            f"主题域「{name}」声明的数据集未声明: {', '.join(undeclared)}"
            "(主题域只能收敛到已声明的数据集;改用正确名字,或先声明该数据集)")
    if not datasets:
        raise ValueError(
            f"主题域「{name}」的 datasets 必填(空作用域 = 域内什么都问不了)")
    topic: dict[str, Any] = {"name": name, "datasets": datasets}
    if payload.get("description"):
        topic["description"] = str(payload["description"])
    syns = _clean_synonyms(payload.get("synonyms"))
    if syns:
        topic["synonyms"] = syns
    metrics = [str(m) for m in (payload.get("metrics") or []) if str(m).strip()]
    if metrics:
        topic["metrics"] = list(dict.fromkeys(metrics))
    examples = [str(e) for e in (payload.get("examples") or []) if str(e).strip()]
    if examples:
        topic["examples"] = examples
    ext = [e for e in (payload.get("custom_extensions") or [])
           if isinstance(e, dict) and e.get("vendor_name")]
    if ext:
        topic["custom_extensions"] = ext
    idx = next((i for i, t in enumerate(model["topics"]) if t.get("name") == name), None)
    if idx is not None:
        old = model["topics"][idx]
        _carryover(old, topic, "description", "synonyms", "metrics", "examples",
                   "custom_extensions")
        model["topics"][idx] = topic
    else:
        model["topics"].append(topic)


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
    elif kind == "topic":
        _apply_topic(model, action, name, payload, dialect)
    else:
        raise ValueError(f"未知草稿类型: {kind}")


# ── 草稿 diff(DiffCard 的 before / after / fields) ─────────


_DIFF_LABELS: dict[str, str] = {
    "name": "名称 name",
    "expression": "表达式 expression",
    "datasets": "锚定数据集",
    "description": "定义 description",
    "definition": "定义 definition",
    "type": "派生类型 type",
    "datatype": "类型 datatype",
    "synonyms": "同义词 synonyms",
    "filter": "过滤 filter",
    "agg_time_dimension": "聚合时间 agg_time_dimension",
    "non_additive": "非可加 non_additive",
    "examples": "示例 examples",
    "custom_extensions": "扩展 custom_extensions",
    "semantic_role": "语义角色 semantic_role",
    "is_time": "时间维度 is_time",
    "enum_display": "枚举展示 enum_display",
    "value_aliases": "值别名 value_aliases",
    "label": "标签 label",
    "source": "物理来源 source",
    "primary_key": "主键 primary_key",
    "unique_keys": "唯一键 unique_keys",
    "row_filter": "行过滤 row_filter",
    "fields": "字段清单 fields",
    "metrics": "收敛指标 metrics",
}

_ACTION_LABELS = {"metric": "指标", "field": "字段", "dataset": "数据集", "topic": "主题域"}


def _present(value: Any) -> bool:
    """视图里「这个键有内容吗」—— 空值不落键,让 diff 只列有信息量的行。

    注意 ``False`` 是**有内容**(``is_time: false`` 与「未声明」是两回事)。
    """
    return value is not None and value != "" and value != [] and value != {}


def _fmt_cell(value: Any) -> str:
    """DiffRow 单元格渲染:列表 `` · `` 连接、缺失为空串(与设计稿原型一致)。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return " · ".join(_fmt_cell(v) for v in value if _present(v))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _raw_expr(entry: dict[str, Any]) -> str:
    """原始 YAML 条目的表达式(第一个非空方言即管理页展示的口径)。"""
    for dia in (entry.get("expression") or {}).get("dialects") or []:
        if isinstance(dia, dict) and str(dia.get("expression") or "").strip():
            return str(dia["expression"])
    return ""


def _find_entity(data: dict[str, Any] | None, kind: str,
                 name: str) -> dict[str, Any] | None:
    """semantics.yml 原始 dict 里的目标实体(**原地引用**,只读不写)。"""
    if not isinstance(data, dict):
        return None
    models = data.get("semantic_model") or []
    model = models[0] if models and isinstance(models[0], dict) else {}
    datasets = [d for d in model.get("datasets", []) or [] if isinstance(d, dict)]
    if kind == "metric":
        return next((m for m in model.get("metrics", []) or []
                     if isinstance(m, dict) and m.get("name") == name), None)
    if kind == "dataset":
        return next((d for d in datasets if d.get("name") == name), None)
    if kind == "topic":
        return next((t for t in model.get("topics", []) or []
                     if isinstance(t, dict) and t.get("name") == name), None)
    if kind == "field":
        ds_name, sep, field_name = name.partition(".")
        if not sep:
            return None
        ds = next((d for d in datasets if d.get("name") == ds_name), None)
        if ds is None:
            return None
        return next((f for f in ds.get("fields", []) or []
                     if isinstance(f, dict) and f.get("name") == field_name), None)
    return None


def _raw_view(kind: str, entry: dict[str, Any]) -> dict[str, Any]:
    """原始条目 → 平铺视图(嵌套的 dialects / ai_context 摊平,便于逐行对比)。"""
    out: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if _present(value):
            out[key] = value

    ai = entry.get("ai_context") if isinstance(entry.get("ai_context"), dict) else {}
    if kind == "metric":
        put("name", str(entry.get("name") or ""))
        put("expression", _raw_expr(entry))
        put("datasets", [str(t) for t in entry.get("datasets") or [] if t])
        put("description", str(entry.get("description") or ""))
        put("type", str(entry.get("type") or ""))
        put("datatype", str(entry.get("datatype") or ""))
        put("synonyms", list(ai.get("synonyms") or []))
        put("examples", list(ai.get("examples") or []))
        put("filter", str(entry.get("filter") or ""))
        put("agg_time_dimension", str(entry.get("agg_time_dimension") or ""))
        put("non_additive", entry.get("non_additive"))
        put("custom_extensions", list(entry.get("custom_extensions") or []))
    elif kind == "field":
        dim = entry.get("dimension") if isinstance(entry.get("dimension"), dict) else {}
        put("name", str(entry.get("name") or ""))
        put("expression", _raw_expr(entry))
        put("datatype", str(entry.get("datatype") or ""))
        put("semantic_role", str(entry.get("semantic_role") or ""))
        put("synonyms", list(ai.get("synonyms") or []))
        put("description", str(entry.get("description") or ""))
        put("is_time", dim.get("is_time"))
        put("enum_display", entry.get("enum_display"))
        put("value_aliases", ai.get("value_aliases"))
        put("label", str(entry.get("label") or ""))
        put("examples", list(ai.get("examples") or []))
        put("custom_extensions", list(entry.get("custom_extensions") or []))
    elif kind == "dataset":
        put("name", str(entry.get("name") or ""))
        put("source", str(entry.get("source") or ""))
        put("primary_key", list(entry.get("primary_key") or []))
        put("unique_keys", entry.get("unique_keys"))
        put("row_filter", str(entry.get("row_filter") or ""))
        put("description", str(entry.get("description") or ""))
        put("synonyms", list(ai.get("synonyms") or []))
        put("examples", list(ai.get("examples") or []))
        put("custom_extensions", list(entry.get("custom_extensions") or []))
        put("fields", [str(f.get("name")) for f in entry.get("fields") or []
                       if isinstance(f, dict) and f.get("name")])
    elif kind == "topic":
        # 主题域是扁平段(name/description/synonyms/datasets/metrics/examples),
        # 不走 ai_context 嵌套(见 ossie.parse_ossie 的 topics 段)。
        put("name", str(entry.get("name") or ""))
        put("datasets", [str(d) for d in entry.get("datasets") or [] if d])
        put("metrics", [str(m) for m in entry.get("metrics") or [] if m])
        put("description", str(entry.get("description") or ""))
        put("synonyms", [str(s) for s in entry.get("synonyms") or [] if s])
        put("examples", [str(e) for e in entry.get("examples") or [] if e])
        put("custom_extensions", list(entry.get("custom_extensions") or []))
    return out


def _draft_diff(data: dict[str, Any] | None, draft: dict[str, Any],
                dialect: str | None = None) -> dict[str, Any]:
    """服务端算草稿 diff —— carryover 语义(未覆盖的手写字段不丢)只在
    ``_apply_draft`` 里,前端重实现必然漂移,所以 before/after 由这里给出。

    形状:``{kind, name, action, before, after, fields[], error}``。
    ``fields`` 行 = 与设计稿原型同形 ``{f, before, after, changed}``(字符串
    单元格);``error`` 是超契约补的键 —— 干跑失败时 before/after 之间的
    差不可得,只留原因,前端据此显示「校验前创建/手工改过 YAML」一类的卡片。
    """
    kind = str(draft.get("kind") or "")
    action = str(draft.get("action") or "")
    name = str(draft.get("name") or "")
    before_entry = _find_entity(data, kind, name)
    before = _raw_view(kind, before_entry) if before_entry is not None else None

    applied = copy.deepcopy(data) if data else {}
    error = ""
    try:
        _apply_draft(applied, draft, dialect)
    except (ValueError, TypeError) as e:
        error = str(e)
    after_entry = _find_entity(applied, kind, name) if not error else None
    after = _raw_view(kind, after_entry) if after_entry is not None else None
    if after is not None and kind == "metric":
        # 写盘不落 datasets 键,但作者显式声明过的锚定要看得见
        extra = [str(t) for t in (draft.get("payload") or {}).get("datasets") or [] if t]
        if extra:
            after["datasets"] = sorted(set(after.get("datasets") or []) | set(extra))

    rows: list[dict[str, Any]] = []
    if not error:
        if action == "delete":
            rows.append({"f": "动作", "before": "存在" if before else "（不存在）",
                         "after": "删除", "changed": bool(before)})
        elif before is None:
            rows.append({"f": "动作", "before": "（不存在）",
                         "after": f"新增{_ACTION_LABELS.get(kind, kind)}", "changed": True})
    b, a = before or {}, after or {}
    for key in sorted(set(b) | set(a)):
        rows.append({
            "f": _DIFF_LABELS.get(key, key),
            "before": _fmt_cell(b.get(key)),
            "after": _fmt_cell(a.get(key)),
            "changed": b.get(key) != a.get(key),
        })
    return {
        "kind": kind, "name": name, "action": action,
        "before": before, "after": after, "fields": rows,
        "error": error or None,
    }


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

    def document(self, datasource: str) -> dict[str, Any]:
        """原始 ``semantics.yml`` dict(只读)。

        validate/preview/diff 需要**未解析的文档**(要判的就是字形与锚定),
        而 ``detail()`` 返回的是解析后的模型 —— 两者不能互相替代。
        文件不存在 → 空 dict(与 ``_load_yaml`` 同语义)。
        """
        path = self._semantics_path(datasource)
        return _load_yaml(path) if path.exists() else {}

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
        """文档级 lint:重复定义/坏表达式/非法关系/脱敏声明错层。

        走 ``lint_semantics_document``(与写盘门禁、git pre-commit 判**同一份
        字节**),而非逐模型 ``lint_semantics`` —— 文档级问题(如 masking 写错
        层)只在文档函数里检查,逐模型检查会静默漏掉它。
        """
        path = self._semantics_path(datasource)
        if not path.exists():
            return []
        data = _load_yaml(path)
        if not data:
            return ["semantics.yml 无法解析(YAML 语法错误)"]
        return lint_semantics_document(data, dialect=dialect or "sqlite")

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
        """管理端详情:模型 + lint(扁平 + 结构化)+ 草稿队列(**每条带 diff**)。

        ``issues`` 保留扁平一版(旧前端兼容),``issue_items`` 是同内容的
        结构化形态(severity/code/target/message/hint);每条 draft 附
        ``diff``(服务端算的 before/after,carryover 语义只有服务端知道)。
        """
        from trove.services.semantic_layer.issues import structured_issues

        path = self._semantics_path(datasource)
        data = _load_yaml(path) if path.exists() else {}
        model = self.model(datasource, dialect)
        issues = self.issues(datasource, dialect)
        drafts = self.drafts(datasource)
        return {
            "enabled": self.enabled(datasource),
            "model": _model_to_dict(model) if model is not None else None,
            "issues": issues,
            "issue_items": structured_issues(issues),
            "drafts": {
                status: [{**e, "diff": _draft_diff(data, e, dialect)} for e in entries]
                for status, entries in drafts.items()
            },
        }

    # ── 审批流写 ──────────────────────────────────────────

    async def create_draft(
        self, datasource: str, kind: str, action: str, name: str,
        payload: dict[str, Any] | None = None, note: str = "",
        actor: str = "",
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
        await self._kb.git_commit(
            datasource, f"semantic: draft {action} {kind} {name}",
            files=["semantic_drafts.yml"],
            trailers={"Generator": "semantic.draft", "Approved-by": actor} if actor else None)
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
        actor: str = "", generator: str = "",
    ) -> dict[str, Any]:
        """审批通过:应用到 semantics.yml → 标记 applied → 刷新镜像。

        ``generator`` 覆盖 git trailer 的 ``Generator``(批量审批走
        ``semantic.batch``,单条默认 ``semantic.confirm``)—— 审计要能分清
        「逐条点的」与「批量点的」,否则批量入口是审计盲区。
        """
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
        # 新建文档补齐 OSSIE v0.2.0.dev0 文档级 version(已存在则保留)
        if "version" not in data and "semantic_model" in data:
            data["version"] = "0.2.0.dev0"
        _reject_bad_semantics(data, dialect)
        _dump_yaml(semantics, data)
        draft["status"] = "applied"
        drafts = self._drafts_with(datasource, draft)
        self._save_drafts(path, drafts)
        await self._kb.force_sync(datasource)
        trailers: dict[str, str] = {}
        if actor or generator:
            trailers["Generator"] = generator or "semantic.confirm"
        if actor:
            trailers["Approved-by"] = actor
        await self._kb.git_commit(
            datasource,
            f"semantic: confirm {draft.get('kind')} {draft.get('name')} "
            f"(draft {draft_id})",
            files=["semantics.yml", "semantic_drafts.yml"],
            lint=self._kb.semantics_lint(datasource, dialect or "sqlite"),
            trailers=trailers or None)
        return dict(draft)

    async def auto_apply(
        self, datasource: str, kind: str, name: str,
        payload: dict[str, Any] | None = None, note: str = "refuse-auto-confirm",
        actor: str = "",
    ) -> dict[str, Any]:
        """A/B 档:机械声明(物理列字段 / 机械聚合指标)直接应用,跳过 pending。

        refuse 节点对通过确定性验证门(物理列存在 / 聚合可编译+真实执行
        shape 过)的 metric/field 草稿直接入库——无需人工确认。复用
        _apply_draft 的全部校验(表达式解析/数据集存在),并在
        semantic_drafts.yml 留 status=applied 的审计记录(可回滚可追溯)。
        """
        self._check_datasource(datasource)
        if kind not in _KINDS:
            raise ValueError(f"kind 必须为 {sorted(_KINDS)} 之一")
        semantics = self._semantics_path(datasource)
        data = _load_yaml(semantics) if semantics.exists() else {}
        entry: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "action": "upsert",
            "name": name,
            "payload": payload or None,
            "note": note or "",
            "status": "applied",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            _apply_draft(data, entry, None)
        except ValueError as e:
            raise ValueError(f"{kind} 自动确认失败: {e}") from e
        if "version" not in data and "semantic_model" in data:
            data["version"] = "0.2.0.dev0"
        _reject_bad_semantics(data, None)
        _dump_yaml(semantics, data)
        path = self._drafts_path(datasource)
        drafts_data = _load_yaml(path)
        drafts = list(drafts_data.get("drafts", [])) if isinstance(drafts_data, dict) else []
        drafts.append(entry)
        _dump_yaml(path, {"drafts": drafts})
        await self._kb.force_sync(datasource)
        await self._kb.git_commit(
            datasource, f"semantic: auto-apply {kind} {name}",
            files=["semantics.yml", "semantic_drafts.yml"],
            lint=self._kb.semantics_lint(datasource),
            trailers={"Generator": "refuse.auto_apply", "Approved-by": actor} if actor else None)
        return dict(entry)

    async def auto_apply_field(
        self, datasource: str, name: str, payload: dict[str, Any] | None = None,
        note: str = "refuse-auto-confirm",
    ) -> dict[str, Any]:
        """A 档便捷入口:字段直接应用(auto_apply 的 field 特化)。"""
        return await self.auto_apply(
            datasource, "field", name, payload=payload, note=note)

    async def reject_draft(self, datasource: str, draft_id: str,
                           actor: str = "", generator: str = "") -> dict[str, Any]:
        """驳回:仅标记 rejected,不改 semantics.yml。"""
        self._check_datasource(datasource)
        draft, path = self._find_draft(datasource, draft_id)
        if draft.get("status") != "pending":
            raise ValueError(f"草稿 {draft_id} 已 {draft.get('status')}")
        draft["status"] = "rejected"
        drafts = self._drafts_with(datasource, draft)
        self._save_drafts(path, drafts)
        await self._kb.force_sync(datasource)
        trailers: dict[str, str] = {}
        if actor or generator:
            trailers["Generator"] = generator or "semantic.reject"
        if actor:
            trailers["Approved-by"] = actor
        await self._kb.git_commit(
            datasource, f"semantic: reject draft {draft_id}",
            files=["semantic_drafts.yml"],
            trailers=trailers or None)
        return dict(draft)

    def _drafts_with(self, datasource: str, updated: dict[str, Any]) -> list[dict[str, Any]]:
        data = _load_yaml(self._drafts_path(datasource))
        drafts = list(data.get("drafts", []) if isinstance(data, dict) else [])
        return [updated if d.get("id") == updated["id"] else d for d in drafts]
