"""semantics.yml 的 OSSIE 格式桥接:flat term 条目 ↔ Apache Ossie semantic_model 文档。

KB 的术语存储(``.trove/kb/<datasource>/semantics.yml``)遵循 Apache Ossie
core spec(semantic_model → datasets/metrics,表达式带 dialects),而 SQLite
镜像与全部下游契约保持扁平的 ``{term, aliases, mapping, tables, definition}``。
本模块是唯一负责两种表示之间转换的地方:

- 写方向(init/learn):flat term dict → OSSIE 文档结构(safe_dump 前)。
- 读方向(sync):OSSIE 文本 → flat term payload(进 kb_items 镜像)。

降级策略:旧 flat ``terms:`` 文件与坏文件一律解析为零条目 + 可操作警告
(不抛异常,``_sync_file`` 借此清掉过期镜像行);空 mapping 条目在写方向
被跳过(parse_ossie 对无方言表达式的 metric 会整体抛错,一个坏 term 不能
炸掉整份文件)。
"""
from __future__ import annotations

import logging
from typing import Any

import yaml
from sqlglot import exp, parse_one

from trove.services.semantic_layer.ossie import parse_ossie

logger = logging.getLogger(__name__)

_ANSI = "ANSI_SQL"
_LEGACY_HINT = "legacy flat terms format — run /kb init --overwrite to migrate"

_DUMP_KWARGS = dict(
    default_flow_style=False, allow_unicode=True, sort_keys=False,
)


def _metric_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """一条 flat term dict → 一个 OSSIE metric dict(写方向共用)。"""
    metric: dict[str, Any] = {
        "name": entry["term"],
        "expression": {"dialects": [{"dialect": _ANSI, "expression": entry["mapping"]}]},
    }
    if entry.get("definition"):
        metric["description"] = entry["definition"]
    aliases = list(entry.get("aliases") or [])
    if aliases:
        metric["ai_context"] = {"synonyms": aliases}
    return metric


def terms_to_ossie_document(
    terms: list[dict[str, Any]], model_name: str = "",
) -> dict[str, Any]:
    """flat term 列表 → OSSIE 文档 dict(``{version, semantic_model}``)。

    datasets 声明 = 各 term ``tables`` 的排序并集——推导锚定依赖声明集
    (``_dataset_refs`` 只信声明过的数据集),缺声明则锚定丢失。
    空/空白 mapping 的条目跳过并告警(parse_ossie 对无 dialect 的 metric
    整体抛错,容忍单个坏 term)。文档带 OSSIE v0.2.0.dev0 ``version``。
    """
    datasets: set[str] = set()
    metrics: list[dict[str, Any]] = []
    for term in terms:
        mapping = str(term.get("mapping", "") or "").strip()
        if not mapping:
            logger.warning("Skipping term %r: empty mapping (OSSIE metrics need an expression)",
                           term.get("term", ""))
            continue
        for t in term.get("tables") or []:
            if t:
                datasets.add(t)
        metrics.append(_metric_from_entry(term))

    model: dict[str, Any] = {"name": model_name, "datasets": [{"name": t} for t in sorted(datasets)]}
    if metrics:
        model["metrics"] = metrics
    return {"version": "0.2.0.dev0", "semantic_model": [model]}


def _parse_model_or_none(text: str) -> Any:
    """Parse OSSIE semantics.yml → SemanticModel, or None + warning.

    与 ossie_to_term_payloads 同哲学:永不抛异常(镜像同步不能因 KB 格式
    问题阻塞)。空白/注释文件静默返回 None;旧 flat ``terms:`` 格式与
    结构坏文件返回 None + 可操作警告。
    """
    try:
        data = yaml.safe_load(text)
    except Exception as e:  # yaml 语法错误
        logger.warning("semantics.yml unreadable (%s) — zero entries loaded", e)
        return None
    if not data:
        return None
    if not isinstance(data, dict) or "terms" in data:
        logger.warning("semantics.yml uses %s — zero entries loaded", _LEGACY_HINT)
        return None
    try:
        return parse_ossie(text)
    except Exception as e:
        logger.warning("semantics.yml not a valid OSSIE semantic model (%s) — "
                       "zero entries loaded; re-run /kb init --overwrite", e)
        return None


def ossie_to_term_payloads(text: str) -> list[dict[str, Any]]:
    """OSSIE 文本 → flat term payload 列表(kind='term' 的镜像条目)。

    永不抛异常(镜像同步不能因 KB 格式问题阻塞):
    - 空白文件 → ``[]`` 静默(合法的占位文件)。
    - 旧 flat ``terms:`` 格式 → ``[]`` + 迁移警告(不兼容决策)。
    - 其他解析失败(无 semantic_model、metric 缺表达式、YAML 坏) → ``[]`` + 警告。
    空 name 的 metric 被跳过:空串子串匹配恒真,会污染 search_terms。
    """
    model = _parse_model_or_none(text)
    if model is None:
        return []

    payloads: list[dict[str, Any]] = []
    for m in model.metrics:
        if not m.name:
            logger.warning("Dropping OSSIE metric with empty name in semantics.yml")
            continue
        payloads.append({
            "term": m.name,
            "aliases": list(m.synonyms),
            "mapping": m.expression,
            "tables": list(m.datasets),
            "definition": m.definition,
        })
    return payloads


def ossie_to_metric_payloads(text: str) -> list[dict[str, Any]]:
    """OSSIE 文本 → 保真 metric payload 列表(kind='metric' 的镜像条目)。

    flat term 只保留 name/aliases/mapping/definition;这里是结构化指标:
    expression / datasets / metric_type / filter / agg_time_dimension /
    non_additive / datatype 全部保留,支撑"指标相关性检索"与 planner 的
    口径注入。降级与 ossie_to_term_payloads 完全一致(共享解析)。
    """
    model = _parse_model_or_none(text)
    if model is None:
        return []

    payloads: list[dict[str, Any]] = []
    for m in model.metrics:
        if not m.name:
            continue  # 已在 term 路径告警,不重复
        payloads.append({
            "name": m.name,
            "aliases": list(m.synonyms),
            "definition": m.definition,
            "expression": m.expression,
            "datasets": list(m.datasets),
            "metric_type": m.metric_type,
            "filter": m.filter,
            "agg_time_dimension": m.agg_time_dimension,
            "non_additive": m.non_additive,
            "datatype": m.datatype,
        })
    return payloads


def ossie_to_entity_payloads(text: str) -> list[dict[str, Any]]:
    """OSSIE 文本 → 实体(维度/枚举字段)payload 列表(kind='entity' 的镜像条目)。

    每个 (dataset, field) 一条:字段名 / 同义词 / 描述 / 枚举值 / 语义角色,
    支撑"维度/值槽"检索与枚举值确认。identifier/time 结构列也入镜像,
    但 name 子串匹配在检索门内排除(避免 "date"/"issued" 撞普通词,与
    schema_linking._semantic_match_datasets 同哲学)。
    """
    model = _parse_model_or_none(text)
    if model is None:
        return []

    payloads: list[dict[str, Any]] = []
    for d in model.datasets:
        for f in d.fields:
            codes = list(f.enum_display.keys())
            labels = [str(f.enum_display[c]) for c in codes]
            payloads.append({
                "field": f.name,
                "dataset": d.name,
                "role": f.semantic_role,
                "description": f.description,
                "synonyms": list(f.synonyms),
                "enum_values": codes,
                "enum_labels": labels,
                "is_time": f.is_time,
                "datatype": f.datatype,
                "label": f.label,
            })
    return payloads


def qualify_mapping(mapping: str, tables: list[str]) -> str:
    """尽力给未限定的列补表前缀(仅当恰好绑定 1 张表)。

    append_term 写路径用:learn/API 草稿可能是裸列名,限定后锚定才能从
    表达式推导。失败/已限定/无列引用(COUNT(*))一律原样返回——限定是
    尽力而为,不允许把合法表达式改坏。
    """
    if len(tables) != 1:
        return mapping
    try:
        tree = parse_one(mapping, read="sqlite")
    except Exception:
        return mapping
    changed = False
    for col in tree.find_all(exp.Column):
        if not col.table:
            col.set("table", exp.to_identifier(tables[0]))
            changed = True
    if not changed:
        return mapping
    try:
        return tree.sql(dialect="sqlite")
    except Exception:
        return mapping


def append_term_to_document(data: dict[str, Any], entry: dict[str, Any]) -> None:
    """把一条 flat term 追加进已加载的 OSSIE 文档 dict(原地修改)。

    dict 级追加而非 parse→re-dump:保留手写内容(多方言条目、
    ai_context.instructions、额外 datasets 声明)。
    """
    models = data.setdefault("semantic_model", [])
    if models:
        model = models[0]
        model.setdefault("datasets", [])
        model.setdefault("metrics", [])
    else:
        model = {"name": "", "datasets": [], "metrics": []}
        models.append(model)

    declared = {d["name"] for d in model["datasets"] if isinstance(d, dict) and d.get("name")}
    for t in entry.get("tables") or []:
        if t and t not in declared:
            model["datasets"].append({"name": t})
            declared.add(t)
    model["metrics"].append(_metric_from_entry(entry))
