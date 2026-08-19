"""Apache Ossie semantic model parser (core spec 0.2.0.dev0 subset).

Reads the subset relevant to NL→SQL context injection:
`semantic_model → datasets` (table anchoring) + `metrics` (name,
multi-dialect expression, ai_context synonyms) + model-level
ai_context instructions. Relationships and custom_extensions are
out of scope for now.
"""
import logging
import re

import yaml
from sqlglot import exp, parse_one

from trove.services.semantic_layer.models import SemanticMetric, SemanticModel

logger = logging.getLogger(__name__)

_ANSI = "ANSI_SQL"

# 正则回退:表达式 SQLGlot 解析失败时提取 数据集.字段 引用
_DATASET_REF_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")


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

    declared = {d.get("name") for d in top.get("datasets", []) or [] if d.get("name")}
    ai = top.get("ai_context") or {}
    instructions = ai.get("instructions", "") if isinstance(ai, dict) else ""

    metrics: list[SemanticMetric] = []
    for m in top.get("metrics", []) or []:
        expression = _pick_expression(
            (m.get("expression") or {}).get("dialects", []), preferred_dialect)
        metric_ai = m.get("ai_context") or {}
        metrics.append(SemanticMetric(
            name=m.get("name", ""),
            expression=expression,
            synonyms=list(metric_ai.get("synonyms", []) or []),
            datasets=_dataset_refs(expression, declared),
            definition=m.get("description", "") or "",
        ))

    return SemanticModel(
        name=top.get("name", "") or "",
        description=top.get("description", "") or "",
        instructions=instructions,
        metrics=metrics,
    )
