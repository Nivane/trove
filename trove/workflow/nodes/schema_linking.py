"""Schema Linking node (语义优先 Phase B) — 唯一通道是语义模型.

语义模型是唯一可答边界:dataset/metric/field 检索(synonym/词重叠,复用
search_terms 语义)→ dataset 锚定 → 产出 semantic_context(仅渲染模型声明,
不含物理 DDL/统计/数值样本)。零命中 = 未覆盖 = 拒绝;无语义模型 = 整体拒绝 +
提示 /kb init。旧裸表路径(catalog 检索/value/FK/对齐/fallback)已从查询图
物理移除(决策 1/2/3)。

Node shape: `async def schema_linking(state: WorkflowState) -> dict`
returns a partial state update.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from trove.prompts import render
from trove.prompts.skills import render_skills
from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.kb.service import KbService, TableNotes, TermHit
from trove.core.logging import get_logger
from trove.llm.observability import record_span
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

# 零匹配兜底的全量表数量上限(金融 dev 集 8 张表;防超长 context)
FALLBACK_TABLES_LIMIT = 8

_QUOTED_RE = re.compile(r"['\"]([^'\"]{2,30})['\"]")
_CAPITALIZED_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")
_ALL_CAPS_RE = re.compile(r"\b[A-Z]{2,}\b")


def _dedup_tables(hits: list[TermHit]) -> list[str]:
    """Flatten term table lists, preserving order and dropping duplicates."""
    names: list[str] = []
    for hit in hits:
        for table in hit.tables:
            if table not in names:
                names.append(table)
    return names


# ── 语义优先通道(Phase B,见 semantic-first 架构文档 §3.1 / §4.1) ──────
#
# 语义模型是唯一可答边界:dataset/metric/field 检索(synonym/词重叠,复用
# search_terms 语义)→ dataset 锚定 → 产出 semantic_context(仅渲染模型声明
# 内容,不含物理 DDL/统计/数值样本)。零命中 = 未覆盖 = 拒绝;无语义模型 =
# 整体拒绝 + 提示 /kb init。旧裸表路径(决策 1)已物理移除。

_SEMANTIC_STOPWORDS = {
    "a", "an", "the", "of", "for", "in", "on", "with", "to", "and", "or",
    "per", "by", "is", "are", "what", "how", "many", "much", "does", "do",
}


def _word_tokens(text: str) -> set[str]:
    """小写词元,去停用词(与 provider/KB term 同款朴素处理)。"""
    words = re.findall(r"[a-z0-9_]+", (text or "").lower())
    return {w for w in words if w not in _SEMANTIC_STOPWORDS}


def _semantic_dataset_score(d: Any, query: str, q_tokens: set[str]) -> float:
    """dataset 名/synonym/description 的确定性匹配分(零 LLM)。

    3.0 = 名称/synonym 子串命中;2.5 = synonym 词重叠 ≥0.5;2.0 =
    description 词重叠 ≥0.5。阈值为 2.0 在调用侧判定。
    """
    q = (query or "").lower()
    if d.name and d.name.lower() in q:
        return 3.0
    for s in d.synonyms:
        if s and str(s).lower() in q:
            return 3.0
    for s in d.synonyms:
        if s:
            st = _word_tokens(s)
            if len(st) >= 2 and len(st & q_tokens) / len(st) >= 0.5:
                return 2.5
    dt = _word_tokens(d.description)
    if len(dt) >= 2 and len(dt & q_tokens) / len(dt) >= 0.5:
        return 2.0
    return 0.0


def _semantic_match_datasets(
    model: Any, query: str, term_hits: list[TermHit],
    semantic_layer: Any | None,
) -> list[str]:
    """模型视角的 dataset 锚定:KB/live term 表 + 词法匹配,排序去重。

    词法匹配覆盖 dataset 名/synonym/description 词重叠,以及**声明字段名/
    synonym 命中**(问题提到某字段 → 锚定其数据集,等价旧列名检索)。
    """
    q_tokens = _word_tokens(query)
    q_lower = (query or "").lower()
    declared = {d.name for d in model.datasets}
    matched: list[str] = []
    scored = [
        (name, _semantic_dataset_score(d, query, q_tokens))
        for d in model.datasets for name in [d.name]
    ]
    for name, score in sorted(scored, key=lambda x: -x[1]):
        if score >= 2.0 and name not in matched:
            matched.append(name)

    # 声明字段命中 → 数据集锚定(问题词 = 字段名/synonym 子串)
    for d in model.datasets:
        if d.name in matched:
            continue
        for f in d.fields:
            if (f.name and f.name.lower() in q_lower) or any(
                s and str(s).lower() in q_lower for s in f.synonyms
            ):
                matched.append(d.name)
                break

    term_tables: list[str] = []
    try:
        term_tables = _dedup_tables(term_hits)
    except Exception:
        pass
    live_tables: list[str] = []
    if semantic_layer is not None:
        try:
            live_tables = _dedup_tables(semantic_layer.terms_for(query))
        except Exception:
            live_tables = []
    for t in term_tables + live_tables:
        if t in declared and t not in matched:
            matched.append(t)
    return matched[:FALLBACK_TABLES_LIMIT]


def _render_semantic_context(
    model: Any, matched: list[str], semantic_layer: Any | None,
    question: str,
) -> str:
    """仅渲染模型声明内容(dataset/metric/field+synonym/关系/instructions)。

    不出现物理 schema 启发(统计/数值样本/FK 命名边/value hints)。
    字段名是已声明语义词表(§4.1),表达式隐藏、由 metric/field_hints 承载。
    """
    from trove.services.semantic_layer.compiler import JoinResolver

    resolution = JoinResolver(model).resolve(list(matched))
    datasets_by_name = {d.name: d for d in model.datasets}
    render_tables = list(matched)
    for extra in resolution.extra_tables:
        if extra not in render_tables:
            render_tables.append(extra)

    parts: list[str] = []
    for name in render_tables:
        d = datasets_by_name.get(name)
        if d is None:
            continue
        lines = [f"Dataset: {name}"]
        if d.description:
            lines.append(f"Description: {d.description}")
        if d.fields:
            lines.append("Fields:")
            for f in d.fields:
                bits = [f.name]
                if f.synonyms:
                    bits.append("synonyms: " + ", ".join(f.synonyms))
                if f.semantic_role:
                    bits.append(f"role={f.semantic_role}")
                if f.is_time:
                    bits.append("time")
                if f.enum_display:
                    # 渲染值映射(不是只报个数):planner 据此把人类值(male/男性)
                    # 落到规范 code(M),编译期再确定性归一——值留在字段层。
                    mapping = ", ".join(
                        f"{k}={v}" for k, v in f.enum_display.items())
                    bits.append(f"enum {{{mapping}}}")
                lines.append("  - " + " | ".join(bits))
        anchored = [m for m in model.metrics if m.datasets and name in m.datasets]
        if anchored:
            lines.append("Metrics:")
            for m in anchored:
                line = f"  - {m.name} = {m.expression}"
                if m.definition:
                    line += f" — {m.definition}"
                lines.append(line)
        parts.append("\n".join(lines))

    resolved_block = "" if resolution.fan_out else JoinResolver.render(resolution)
    if resolved_block:
        parts.append(resolved_block)

    model_lines: list[str] = []
    agnostic = [m for m in model.metrics if not m.datasets]
    if agnostic:
        model_lines.append(
            "Metrics:\n" + "\n".join(
                f"- {m.name} = {m.expression}" for m in agnostic))
    if model.instructions:
        model_lines.append(f"Semantic note: {model.instructions}")
    if model_lines:
        parts.append("\n\n".join(model_lines))

    if semantic_layer is not None:
        try:
            hits = semantic_layer.field_hits(question, matched)
            if hits:
                parts.append("Field hints: " + "; ".join(hits))
        except Exception:
            pass

    return "\n\n".join(parts) if parts else "No semantic model matched this question."


async def _semantic_linking(
    state: WorkflowState, kb, connectors, semantic_layer,
    term_hits: list[TermHit], search_query: str, datasource: str,
) -> dict[str, Any]:
    """语义优先主通道(§3.1):dataset 锚定 + semantic_context,唯一路径。"""
    model = None
    if semantic_layer is not None and datasource and getattr(
            semantic_layer, "enabled", False):
        try:
            model = semantic_layer.model()
        except Exception as e:
            logger.warning("Semantic model lookup failed (%s): %s", datasource, e)
            model = None

    if model is None:
        # 决策 2/3:无语义模型 → 整体拒绝 + 提示 /kb init(不静默降级裸表)
        return {
            "matched_tables": [],
            "schema_context": "",
            "semantic_context": "",
            "no_model": True,
            "link_detail": {"semantic_first": True, "no_model": True},
        }

    matched = _semantic_match_datasets(model, search_query, term_hits, semantic_layer)
    semantic_context = _render_semantic_context(model, matched, semantic_layer, state.question)

    base: dict[str, Any] = {
        "matched_tables": matched,
        "schema_context": semantic_context,
        "semantic_context": semantic_context,
        "link_detail": {
            "semantic_first": True,
            "matched_datasets": list(matched),
        },
    }
    if not matched:
        # 零命中 = 未覆盖 = 拒绝(决策 4),不 fallback 全量表
        base["refusal"] = {
            "reason": "no_semantic_match",
            "question": state.question,
            "semantic_context": semantic_context,
        }
    return base










def make_schema_linking(
    kb: KbService | None = None,
    connectors: ConnectorRegistry | None = None,
    semantic_layer: Any | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the schema_linking node — 语义优先(Phase B)唯一通道。

    Args:
        kb: Optional knowledge base for term matching (metric 锚定数据集)。
        connectors: Registry providing the active datasource name (KB scope).
        semantic_layer: Live semantic provider — 唯一可答边界;无语义模型
            → no_model 拒绝(决策 2/3)。

    Returns:
        Async node function taking WorkflowState and returning a partial update.
    """

    async def schema_linking(state: WorkflowState) -> dict[str, Any]:
        # Upstream node failed — pass through without running
        if state.error:
            return {}

        # Knowledge base is scoped to the active datasource
        datasource = state.datasource or (
            connectors.default_name if connectors is not None else ""
        )

        # 带上下文重跑：诊断文本参与检索，诊断中提到的表/术语可重新进入匹配
        search_query = (
            (state.question + "\n" + state.error_analysis).strip()
            if state.error_analysis else state.question
        )

        # 1. Knowledge base term matching (substring, works for Chinese)
        term_hits: list[TermHit] = []
        if kb is not None and datasource:
            await kb.ensure_synced(default_datasource=datasource)
            term_hits = await kb.search_terms(search_query, datasource)

        update = await _semantic_linking(
            state, kb, connectors, semantic_layer, term_hits, search_query, datasource,
        )

        if term_hits:
            hits = [
                {
                    "kind": "term",
                    "term": h.term,
                    "mapping": h.mapping,
                    "definition": h.definition,
                    "tables": h.tables,
                }
                for h in term_hits
            ]
            update["kb_hits"] = hits
            # KB 术语命中进 langfuse(无 Langfuse 时 no-op)
            with record_span(
                "kb.hits", input={"question": state.question},
            ) as span:
                if span is not None:
                    span.update(output={
                        "hits": [{"term": h["term"], "mapping": h["mapping"]}
                                 for h in hits],
                    })
        return update

    return schema_linking
