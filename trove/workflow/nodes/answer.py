"""Intent answer nodes — direct responses for non-query intents.

Each factory produces an async node that renders an intent_answer
string from catalog/KB data (no LLM involved):

  - answer_schema:   table inventory with KB annotations
  - answer_semantic: business term definitions (question substring match)
  - answer_knowledge: KB item counts + term/example listings
  - answer_lineage:  join relationships via *_id heuristics
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.kb.service import KbService
from trove.workflow.nodes.schema_linking import _join_hints
from trove.core.i18n import L, detect_language
from trove.workflow.state import WorkflowState


def _datasource(connectors: ConnectorRegistry | None) -> str:
    if connectors is None:
        return ""
    return connectors.default_name or ""


def make_answer_schema(
    catalog: CatalogService | None = None,
    kb: KbService | None = None,
    connectors: ConnectorRegistry | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    async def answer_schema(state: WorkflowState) -> dict[str, Any]:
        lang = detect_language(state.question)
        if catalog is None:
            return {"intent_answer": L(lang, "当前没有可用的数据源目录。", "No datasource catalog available.")}
        tables = await catalog.list_tables()
        if not tables:
            return {"intent_answer": L(lang, "数据源中没有任何表。", "The datasource has no tables.")}

        notes = {}
        ds = _datasource(connectors)
        if kb is not None and ds:
            notes = await kb.table_notes(
                [t["name"] for t in tables], ds,
            )
        if lang == "zh":
            lines = [f"数据源共 {len(tables)} 张表："]
        else:
            lines = [f"The datasource has {len(tables)} tables:"]
        for t in tables:
            if lang == "zh":
                lines.append(f"- {t['name']}（{t['columns']} 列，约 {t.get('row_count', '?')} 行）")
            else:
                lines.append(f"- {t['name']} ({t['columns']} columns, ~{t.get('row_count', '?')} rows)")
            table_notes = notes.get(t["name"])
            if table_notes and table_notes.description:
                lines.append(L(lang, f"  描述：{table_notes.description}", f"  Description: {table_notes.description}"))
        return {"intent_answer": "\n".join(lines)}

    return answer_schema


def make_answer_semantic(
    kb: KbService | None = None,
    connectors: ConnectorRegistry | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    async def answer_semantic(state: WorkflowState) -> dict[str, Any]:
        lang = detect_language(state.question)
        if kb is None:
            return {"intent_answer": L(lang, "知识库未启用。", "Knowledge base is not enabled.")}
        ds = _datasource(connectors)
        if not ds:
            return {"intent_answer": L(lang, "当前没有激活的数据源。", "No active datasource.")}
        await kb.ensure_synced(ds)
        hits = await kb.search_terms(state.question, ds)
        if not hits:
            return {
                "intent_answer": L(
                    lang,
                    "知识库中暂无与你的问题匹配的术语。可以先用 /kb learn 沉淀，或查看 /kb list。",
                    "No terms in the knowledge base match your question. "
                    "Use /kb learn to add one, or check /kb list.",
                ),
            }
        lines = [L(lang, "匹配的术语口径：", "Matching term definitions:")]
        for h in hits:
            lines.append(f"- {h.term} → {h.mapping}")
            if h.definition:
                lines.append(L(lang, f"  定义：{h.definition}", f"  Definition: {h.definition}"))
            if h.tables:
                lines.append(L(lang, f"  涉及表：{', '.join(h.tables)}", f"  Tables: {', '.join(h.tables)}"))
        return {"intent_answer": "\n".join(lines)}

    return answer_semantic


def make_answer_knowledge(
    kb: KbService | None = None,
    connectors: ConnectorRegistry | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    async def answer_knowledge(state: WorkflowState) -> dict[str, Any]:
        lang = detect_language(state.question)
        if kb is None:
            return {"intent_answer": L(lang, "知识库未启用。", "Knowledge base is not enabled.")}
        ds = _datasource(connectors)
        if not ds:
            return {"intent_answer": L(lang, "当前没有激活的数据源。", "No active datasource.")}
        await kb.ensure_synced(ds)
        counts = (await kb.list_items()).get(ds, {})
        if not counts:
            return {"intent_answer": L(lang, "知识库为空。运行 /kb init 初始化。", "The knowledge base is empty. Run /kb init.")}

        terms = await kb.list_term_names(ds)
        examples = await kb.list_example_questions(ds)
        lines = [L(lang, f"知识库（{ds}）当前内容：", f"Knowledge base ({ds}) contents:")]
        lines.append(L(lang, f"- 表注释：{counts.get('table', 0)} 张表", f"- Table annotations: {counts.get('table', 0)}"))
        lines.append(L(lang, f"- 业务术语：{counts.get('term', 0)} 条", f"- Business terms: {counts.get('term', 0)}"))
        lines.append(L(lang, f"- 参考 SQL / 模板：{counts.get('example', 0) + counts.get('template', 0)} 条", f"- Reference SQL / templates: {counts.get('example', 0) + counts.get('template', 0)}"))
        if terms:
            lines.append(L(lang, "\n术语列表：", "\nTerms:"))
            lines.extend(f"  · {t}" for t in terms[:20])
        if examples:
            lines.append(L(lang, "\n示例/模板：", "\nExamples/templates:"))
            lines.extend(f"  · {q}" for q in examples[:10])
        return {"intent_answer": "\n".join(lines)}

    return answer_knowledge


def make_answer_lineage(
    catalog: CatalogService | None = None,
    connectors: ConnectorRegistry | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    async def answer_lineage(state: WorkflowState) -> dict[str, Any]:
        lang = detect_language(state.question)
        if connectors is None:
            return {"intent_answer": L(lang, "当前没有激活的数据源。", "No active datasource.")}
        schema = await connectors.get_schema()
        table_columns = {
            t.name: [c.name for c in t.columns] for t in schema.tables
        }

        # 从问题中提取提及的表名（中英文表名直接子串匹配）
        mentioned = [
            name for name in table_columns if name.lower() in state.question.lower()
        ]
        targets = mentioned or list(table_columns)

        lines = []
        for table in targets:
            hints = _join_hints(table, table_columns[table], table_columns)
            if hints:
                lines.append(L(lang, f"{table} 的关联：", f"Relationships of {table}:"))
                lines.extend(f"  · {h}" for h in hints)
        if not lines:
            return {"intent_answer": L(lang, "未发现表间关联（列名没有 *_id 外键样式）。", "No table relationships found (no *_id foreign-key-style columns).")}
        if not mentioned:
            lines.insert(0, L(lang, f"全库关联总览（{len(schema.tables)} 张表）：", f"All relationships ({len(schema.tables)} tables):"))
        else:
            lines.insert(0, L(lang, f"「{'、'.join(mentioned)}」的血缘关系：", f"Lineage of {'/'.join(mentioned)}:"))
        return {"intent_answer": "\n".join(lines)}

    return answer_lineage
