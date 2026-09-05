"""Apache Ossie semantic model parser (core spec, v0.2.0.dev0).

Reads the parts relevant to NL→SQL plus the spec's extensibility surface:
- `semantic_model → datasets` (source, primary_key, unique_keys, fields
  with datatype / dimension.is_time / label / synonyms) — table anchoring
  + the declared filter/group-by vocabulary;
- `relationships` (from → to, key pairs) — the declared join graph;
- `metrics` (name, multi-dialect aggregate expression, datatype,
  ai_context synonyms/examples) — business metrics;
- `custom_extensions` (vendor_name + data) — preserved/passed through;
- model-level `ai_context` (instructions / synonyms / examples) and the
  top-level `version` string.
"""
import logging
import re

import yaml
from sqlglot import exp, parse_one

from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
    TimeSpine,
    _clean_extensions,
)

logger = logging.getLogger(__name__)

_ANSI = "ANSI_SQL"

# 正则回退:表达式 SQLGlot 解析失败时提取 数据集.字段 引用
_DATASET_REF_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")

# OSSIE is_time 默认:时态 datatype 未显式覆盖时视为时间维度
_TEMPORAL_TYPES = {"date", "time", "datetime", "datetimetz"}


def _pick_expression(dialects: list[dict], preferred: str) -> str:
    """从方言列表中选表达式:adapter 方言 → ANSI_SQL → 第一个可用。

    Raises:
        ValueError: 没有任何可用表达式(结构性问题,provider 层会
            last-known-good 兜底)。
    """
    by_dialect = {
        d.get("dialect", "").upper(): d.get("expression", "")
        for d in dialects if d.get("expression")
    }
    for key in (preferred.upper(), _ANSI):
        if key in by_dialect:
            return by_dialect[key]
    if not by_dialect:
        raise ValueError("metric expression has no dialects")
    return next(iter(by_dialect.values()))


def _expression_of(entity: dict, preferred: str) -> str:
    """实体(dataset 下 field / metric)的 expression.dialects 抽取。"""
    expr = (entity.get("expression") or {}).get("dialects", []) or []
    if not expr:
        raise ValueError("expression has no dialects")
    return _pick_expression(expr, preferred)


def _dataset_refs(expression: str, declared: set[str]) -> list[str]:
    """表达式里引用到的数据集名(只信声明过的数据集,防误报)。"""
    found: set[str] = set()
    try:
        for col in parse_one(expression, read="sqlite").find_all(exp.Column):
            if col.table in declared:
                found.add(col.table)
    except Exception:  # 片段解析失败 → 正则回退
        for m in _DATASET_REF_RE.finditer(expression):
            if m.group(1) in declared:
                found.add(m.group(1))
    return sorted(found)


def _resolve_is_time(dimension: object, datatype: str | None) -> bool:
    """dimension.is_time 显式值优先;未设置时按 datatype 时态默认。"""
    if isinstance(dimension, dict) and "is_time" in dimension:
        return bool(dimension["is_time"])
    if datatype and datatype.lower() in _TEMPORAL_TYPES:
        return True
    return False


def _ai_context(entity: dict) -> tuple[str, list[str], list[str]]:
    """实体/模型的 ai_context → (instructions, synonyms, examples)。

    兼容旧形态:ai_context 为字符串 → 视为 synonyms。
    """
    ai = entity.get("ai_context")
    if isinstance(ai, str):
        return "", [ai], []
    if isinstance(ai, dict):
        return (
            str(ai.get("instructions", "") or ""),
            list(ai.get("synonyms") or []),
            list(ai.get("examples") or []),
        )
    return "", [], []


def parse_ossie(text: str, preferred_dialect: str = "ansi_sql") -> SemanticModel:
    """Parse an Ossie semantic model YAML into SemanticModel.

    Args:
        text: OSSIE YAML content (spec: top-level `semantic_model` list).
        preferred_dialect: active adapter dialect; expression picking
            prefers it, then ANSI_SQL, then the first available.

    Raises:
        ValueError: no `semantic_model` entry or a metric has no
            expression (structural problems, not per-metric drops).
    """
    data = yaml.safe_load(text) or {}
    models = data.get("semantic_model") or []
    if not models:
        raise ValueError("no semantic_model found in OSSIE YAML")
    if len(models) > 1:
        logger.warning("OSSIE file has %d semantic models; using the first", len(models))
    top = models[0]
    if not isinstance(top, dict):
        raise ValueError("semantic_model entry must be a mapping")
    version = str(data.get("version") or "").strip()

    declared = {d.get("name") for d in top.get("datasets", []) or [] if d.get("name")}

    datasets: list[SemanticDataset] = []
    for d in top.get("datasets", []) or []:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        name = d["name"]
        _, ds_synonyms, ds_examples = _ai_context(d)
        fields: list[SemanticField] = []
        for f in d.get("fields", []) or []:
            if not isinstance(f, dict) or not f.get("name"):
                continue
            try:
                expr = _expression_of(f, preferred_dialect)
            except ValueError as e:
                logger.warning(
                    "Dropping field %s.%s: %s", name, f.get("name"), e)
                continue
            dt = f.get("datatype") or None
            dim = f.get("dimension")
            _, f_synonyms, f_examples = _ai_context(f)
            # P5.1:语义角色 + 枚举 display 字典(均可选;缺省由生成/消费端推导)
            role = str(f.get("semantic_role") or "").strip().lower()
            display = f.get("enum_display") or {}
            enum_display = {
                str(k): str(v) for k, v in display.items()
                if isinstance(display, dict)
            } if isinstance(display, dict) else {}
            fields.append(SemanticField(
                name=f["name"],
                expression=expr,
                datatype=(str(dt) if dt else None),
                is_time=_resolve_is_time(dim, dt),
                description=f.get("description", "") or "",
                synonyms=f_synonyms,
                semantic_role=role,
                enum_display=enum_display,
                label=str(f.get("label", "") or "").strip(),
                examples=f_examples,
                custom_extensions=_clean_extensions(f.get("custom_extensions")),
            ))
        datasets.append(SemanticDataset(
            name=name,
            source=str(d.get("source") or ""),
            primary_key=[str(pk) for pk in (d.get("primary_key") or [])],
            unique_keys=[
                [str(k) for k in keys]
                for keys in (d.get("unique_keys") or [])
                if isinstance(keys, list) and keys
            ],
            description=d.get("description", "") or "",
            synonyms=ds_synonyms,
            fields=fields,
            examples=ds_examples,
            custom_extensions=_clean_extensions(d.get("custom_extensions")),
        ))

    relationships: list[SemanticRelationship] = []
    for r in top.get("relationships", []) or []:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        from_cols = [str(c) for c in (r.get("from_columns") or [])]
        to_cols = [str(c) for c in (r.get("to_columns") or [])]
        if r.get("from") not in declared or r.get("to") not in declared:
            logger.warning(
                "Dropping relationship %s: endpoint not in declared datasets",
                r.get("name"),
            )
            continue
        if not from_cols or len(from_cols) != len(to_cols):
            logger.warning(
                "Dropping relationship %s: from_columns/to_columns mismatch",
                r.get("name"),
            )
            continue
        _, r_examples, _ = _ai_context(r)
        relationships.append(SemanticRelationship(
            name=r["name"],
            from_=str(r["from"]),
            to=str(r["to"]),
            from_columns=from_cols,
            to_columns=to_cols,
            cardinality=str(r.get("cardinality") or "").strip().upper(),
            fan_out=str(r.get("fan_out") or "").strip().lower(),
            examples=r_examples,
            custom_extensions=_clean_extensions(r.get("custom_extensions")),
        ))

    ai = top.get("ai_context") or {}
    instructions_txt = ai.get("instructions", "") if isinstance(ai, dict) else ""
    model_examples = ai.get("examples", []) if isinstance(ai, dict) else []

    spine = None
    ts = top.get("time_spine") or {}
    if isinstance(ts, dict) and ts.get("field"):
        grain = str(ts.get("granularity") or "month").strip().lower()
        if grain not in {"year", "quarter", "month", "week", "day"}:
            logger.warning("time_spine.granularity %r invalid; defaulting to month", grain)
            grain = "month"
        fill = str(ts.get("fill") or "none").strip().lower()
        if fill not in {"none", "0", "previous"}:
            fill = "none"
        spine = TimeSpine(
            field=str(ts["field"]).strip(),
            granularity=grain,
            fill=fill,
        )

    metrics: list[SemanticMetric] = []
    for m in top.get("metrics", []) or []:
        # 与原实现一致:metric 缺可用的 expression 是结构性问题 → 抛错
        # (provider 层 last-known-good 兜底);非法/坏结构的表达式才逐条丢弃。
        expression = _expression_of(m, preferred_dialect)
        _, synonyms, examples = _ai_context(m)
        m_datatype = m.get("datatype") or None
        metrics.append(SemanticMetric(
            name=m.get("name", ""),
            expression=expression,
            synonyms=synonyms,
            datasets=_dataset_refs(expression, declared),
            definition=m.get("description", "") or "",
            metric_type=str(m.get("type") or "").strip().lower(),
            filter=str(m.get("filter") or "").strip(),
            agg_time_dimension=str(m.get("agg_time_dimension") or "").strip(),
            non_additive=bool(m.get("non_additive") or False),
            datatype=(str(m_datatype) if m_datatype else None),
            examples=examples,
            custom_extensions=_clean_extensions(m.get("custom_extensions")),
        ))

    return SemanticModel(
        name=top.get("name", "") or "",
        description=top.get("description", "") or "",
        instructions=instructions_txt,
        metrics=metrics,
        datasets=datasets,
        relationships=relationships,
        version=version,
        examples=model_examples,
        custom_extensions=_clean_extensions(top.get("custom_extensions")),
        time_spine=spine,
    )
