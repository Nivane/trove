"""Metadata answer node — LLM-composed answers from metadata context.

The route node decides query vs metadata; HERE the LLM decides what the
user actually asked (relationships? table meanings? calibers? several
things at once?) and composes a focused answer from the assembled
metadata context — no unbounded signal-word matching.

Lineage questions (血缘/来源/上游/下游/怎么算) get an OPTIONAL
deterministic lineage section assembled from LineageService state — the
LLM composes prose around it, or the fallback renders it directly.

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
from trove.services.lineage.service import LineageService
from trove.services.lineage.render import (
    render_column_lineage,
    render_table_downstream,
    render_table_upstream,
)
from trove.workflow.nodes.schema_linking import _join_hints
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

_RELATIONS_RE = re.compile(r"关系|关联|关连|连接|相连|怎么连|血缘|来源|从哪")
_LINEAGE_RE = re.compile(
    r"血缘|数据来源|上游|下游|怎么算|如何计算|如何算出|计算过程|算出|盏生|依赖|取自|来自|追溯|数据流|链路"
)
_TERMS_RE = re.compile(r"口径|定义|含义|是什么意思|指标")
_KB_RE = re.compile(r"知识库|模板|示例|参考")
_INVENTORY_RE = re.compile(r"有哪些表|表结构|list\\s+tables|\\btables\\b|\\bschema\\b")
_TABLE_COL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*[\..、]\s*([A-Za-z_][A-Za-z0-9_]*)")


def _datasource(connectors: ConnectorRegistry | None, datasource: str = "") -> str:
    if connectors is None:
        return ""
    return datasource or connectors.default_name or ""


async def _lineage_targets(
    question: str, catalog: CatalogService | None,
    extra_tables: list[str] | None = None,
) -> list[tuple[str, str | None]]:
    """Extract (table, column|None) lineage targets from a question.

    Three match tiers, deterministic:
      1. explicit ``table.column`` / ``table·column`` forms,
      2. an existing table name mentioned verbatim in the question
         (physical catalog ∪ lineage-known names: views/ETL outputs),
      3. a column name that exists in exactly one catalog table.
    """
    targets: list[tuple[str, str | None]] = []
    tables = await catalog.list_tables() if catalog else []
    known = {t["name"].lower(): t["name"] for t in tables}
    for extra in extra_tables or []:
        known.setdefault(extra.lower(), extra)

    for match in _TABLE_COL_RE.finditer(question):
        tc, col = match.group(1).lower(), match.group(2).lower()
        if tc in known:
            targets.append((known[tc], col))

    mentioned = [t for t in tables if t["name"].lower() in question.lower()]
    seen = {t.lower() for t, _ in targets}
    for t in mentioned:
        if t["name"].lower() not in seen:
            targets.append((t["name"], None))
            seen.add(t["name"].lower())
    for extra in extra_tables or []:
        if extra.lower() in question.lower() and extra.lower() not in seen:
            targets.append((extra, None))
            seen.add(extra.lower())

    if not targets and catalog:
        # Column-only mention → resolve against the catalog (unambiguous only)
        for t in tables:
            detail = await catalog.table_detail(t["name"])
            cols = {c["name"].lower() for c in (detail or {}).get("columns", [])}
            hit = [q for q in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", question) if q.lower() in cols]
            if len(hit) == 1:
                targets.append((t["name"], hit[0]))
                break
    return targets[:3]


def _has_lineage_signal(question: str) -> bool:
    return bool(_LINEAGE_RE.search(question))


async def _lineage_context(
    question: str, catalog, connectors, lineage: LineageService | None,
    datasource: str = "",
) -> str:
    """Deterministic lineage material for the metadata LLM prompt."""
    if lineage is None:
        return ""
    ds = _datasource(connectors, datasource)
    if not ds:
        return ""
    extra: list[str] = []
    try:
        extra = await lineage.known_tables(ds)
    except Exception as e:
        logger.warning("lineage known_tables failed: %s", e)
    targets = await _lineage_targets(question, catalog, extra)
    if not targets:
        return ""
    sections: list[str] = []
    for table, column in targets:
        if column:
            cell = await lineage.column_lineage(ds, table, column)
            sections.append(render_column_lineage(table, column, cell))
            consumers = cell.get("consumers", [])
            if not consumers:
                upstream = await lineage.table_upstream(ds, table)
                if upstream:
                    sections.append(render_table_upstream(ds, table, upstream))
        else:
            upstream = await lineage.table_upstream(ds, table)
            if upstream:
                sections.append(render_table_upstream(ds, table, upstream))
            downstream = await lineage.table_downstream(ds, table)
            sections.append(render_table_downstream(table, downstream))
    return "\n\n".join(sections)[:4000]


async def _build_metadata_context(
    state: WorkflowState, catalog, kb, connectors, lineage: LineageService | None = None,
) -> str:
    """All metadata material the LLM may need (bounded)."""
    sections: list[str] = []
    ds = _datasource(connectors, state.datasource)
    tables = await catalog.list_tables() if catalog else []
    mentioned = [t for t in tables if t["name"].lower() in state.question.lower()]

    # 血缘(确定性,零 LLM):血缘信号问题附 LineageService 事实
    if _has_lineage_signal(state.question):
        lg = await _lineage_context(
            state.question, catalog, connectors, lineage, state.datasource,
        )
        if lg:
            sections.append(lg)
        else:
            sections.append("Lineage: no recorded lineage facts.")

    # 提及表详情
    if mentioned and catalog:
        for t in mentioned[:5]:
            detail = await catalog.table_detail(t["name"])
            cols = ", ".join(f"{c['name']} {c['type']}" for c in (detail or {}).get("columns", []))
            sections.append(f"Table {t['name']}: {cols}")

    # 全库关联总览
    if connectors:
        schema = await connectors.get_schema(state.datasource or None)
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


async def _fallback_answer(
    state: WorkflowState, catalog, kb, connectors, lineage: LineageService | None = None,
) -> str:
    """Template composite answer (no LLM / LLM failure)."""
    lang = state.lang
    q = state.question
    sections: list[str] = []
    tables = await catalog.list_tables() if catalog else []
    mentioned = [t for t in tables if t["name"].lower() in q.lower()]
    ds = _datasource(connectors, state.datasource)

    # 血缘(确定性,零 LLM,渲染即答案)
    if _has_lineage_signal(q):
        lg = await _lineage_context(
            q, catalog, connectors, lineage, state.datasource,
        )
        if lg:
            sections.append(lg)
        else:
            sections.append(L(lang, "尚无该对象的数据血缘记录。", "No lineage records for this object yet."))

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
        schema = await connectors.get_schema(state.datasource or None)
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
    lineage: LineageService | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    async def answer_metadata(state: WorkflowState) -> dict[str, Any]:
        context = await _build_metadata_context(state, catalog, kb, connectors, lineage)

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

        return {"intent_answer": await _fallback_answer(state, catalog, kb, connectors, lineage)}

    return answer_metadata
