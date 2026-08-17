"""Metadata answer node — LLM-composed answers from metadata context.

The route node decides query vs metadata; HERE the LLM decides what the
user actually asked (relationships? table meanings? calibers? several
things at once?) and composes a focused answer from the assembled
metadata context — no unbounded signal-word matching.

Fallback (no LLM or LLM failure): the template-based composite answer
below keeps the pipeline responsive.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.i18n import L
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.prompts import render
from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.kb.service import KbService
from trove.workflow.nodes.schema_linking import _join_hints
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

_RELATIONS_RE = re.compile(r"关系|关联|关连|连接|相连|怎么连|血缘|来源|从哪")
_TERMS_RE = re.compile(r"口径|定义|含义|是什么意思|指标")
_KB_RE = re.compile(r"知识库|模板|示例|参考")
_INVENTORY_RE = re.compile(r"有哪些表|表结构|list\\s+tables|\\btables\\b|\\bschema\\b")


def _datasource(connectors: ConnectorRegistry | None) -> str:
    if connectors is None:
        return ""
    return connectors.default_name or ""


async def _build_metadata_context(
    state: WorkflowState, catalog, kb, connectors,
) -> str:
    """All metadata material the LLM may need (bounded)."""
    sections: list[str] = []
    ds = _datasource(connectors)
    tables = await catalog.list_tables() if catalog else []
    mentioned = [t for t in tables if t["name"].lower() in state.question.lower()]

    # 提及表详情
    if mentioned and catalog:
        for t in mentioned[:5]:
            detail = await catalog.table_detail(t["name"])
            cols = ", ".join(f"{c['name']} {c['type']}" for c in (detail or {}).get("columns", []))
            sections.append(f"Table {t['name']}: {cols}")

    # 全库关联总览
    if connectors:
        schema = await connectors.get_schema()
        table_columns = {t.name: [c.name for c in t.columns] for t in schema.tables}
        rel_lines = []
        for table, cols in table_columns.items():
            for hint in _join_hints(table, cols, table_columns):
                rel_lines.append(hint)
        if rel_lines:
            sections.append("Relationships: " + "; ".join(rel_lines))

    # 术语
    if kb is not None and ds:
        await kb.ensure_synced(ds)
        terms = await kb.list_term_names(ds)
        if terms:
            sections.append("Business terms: " + ", ".join(terms[:30]))

    # 表清单
    if tables:
        sections.append("All tables: " + ", ".join(t["name"] for t in tables))

    return "\n".join(sections)[:6000]


async def _fallback_answer(state: WorkflowState, catalog, kb, connectors) -> str:
    """Template composite answer (no LLM / LLM failure)."""
    lang = state.lang
    q = state.question
    sections: list[str] = []
    tables = await catalog.list_tables() if catalog else []
    mentioned = [t for t in tables if t["name"].lower() in q.lower()]
    ds = _datasource(connectors)

    if mentioned and catalog:
        notes = {}
        if kb is not None and ds:
            notes = await kb.table_notes([t["name"] for t in mentioned], ds)
        for t in mentioned[:5]:
            lines = [L(lang, f"表 {t['name']}（{t['columns']} 列）：", f"Table {t['name']} ({t['columns']} columns):")]
            detail = await catalog.table_detail(t["name"])
            for col in (detail or {}).get("columns", []):
                lines.append(f"  • {col['name']} ({col['type']})")
            sections.append("\n".join(lines))

    if _RELATIONS_RE.search(q) and connectors:
        schema = await connectors.get_schema()
        table_columns = {t.name: [c.name for c in t.columns] for t in schema.tables}
        targets = [t["name"] for t in mentioned] or list(table_columns)
        rel_lines: list[str] = []
        for table in targets[:8]:
            for hint in _join_hints(table, table_columns.get(table, []), table_columns):
                rel_lines.append(f"  • {hint}")
        if rel_lines:
            sections.append(L(lang, "关联关系：", "Relationships:") + "\n" + "\n".join(rel_lines))

    if _TERMS_RE.search(q) and kb is not None and ds:
        await kb.ensure_synced(ds)
        hits = await kb.search_terms(q, ds)
        if hits:
            sections.append("\n".join(f"  • {h.term} → {h.mapping}" for h in hits))

    if _KB_RE.search(q) and kb is not None and ds:
        await kb.ensure_synced(ds)
        counts = (await kb.list_items()).get(ds, {})
        if counts:
            sections.append(L(lang, f"知识库：表注释 {counts.get('table', 0)} / 术语 {counts.get('term', 0)} / 示例 {counts.get('example', 0) + counts.get('template', 0)}", f"Knowledge base: tables {counts.get('table', 0)} / terms {counts.get('term', 0)} / examples {counts.get('example', 0) + counts.get('template', 0)}"))

    if _INVENTORY_RE.search(q) or not sections:
        if catalog:
            inv = [L(lang, f"数据源共 {len(tables)} 张表：", f"The datasource has {len(tables)} tables:")]
            inv.extend(f"  • {t['name']}（{t['columns']} 列）" if lang == "zh" else f"  • {t['name']} ({t['columns']} columns)" for t in tables)
            sections.append("\n".join(inv))

    return "\n\n".join(sections) if sections else L(lang, "当前没有可用的数据源目录。", "No datasource catalog available.")


def make_answer_metadata(
    catalog: CatalogService | None = None,
    kb: KbService | None = None,
    connectors: ConnectorRegistry | None = None,
    llm: LLMGateway | None = None,
    config: AgentConfig | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    async def answer_metadata(state: WorkflowState) -> dict[str, Any]:
        context = await _build_metadata_context(state, catalog, kb, connectors)

        if llm is not None:
            try:
                system_prompt = render("answer/system", lang=state.lang)
                model = (config.target if config else "") or "openai/gpt-4o"
                response = await llm.chat(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": render(
                            "answer/user",
                            lang=state.lang,
                            context=context,
                            question=state.question,
                            error_feedback=state.error_feedback,
                        )},
                    ],
                    metadata={
                        "node": "answer_metadata",
                        "session_id": state.session_id,
                        "run_id": state.run_id,
                        "question": state.question[:80],
                    },
                )
                answer = response.strip()
                if answer:
                    return {"intent_answer": answer}
            except Exception as e:
                logger.warning("Metadata LLM answer failed, using fallback: %s", e)

        return {"intent_answer": await _fallback_answer(state, catalog, kb, connectors)}

    return answer_metadata
