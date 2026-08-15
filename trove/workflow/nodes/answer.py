"""Metadata answer node — composite answers for "about the data" questions.

Instead of routing metadata questions into single-purpose buckets, the
node combines every signal found in the question:

  - mentioned table names → per-table details (columns + annotations)
  - relationship signals (关系/关联/血缘/来源) → join relationships
  - term signals (口径/定义/含义/指标) → matching business terms
  - KB signals (知识库/模板/示例) → knowledge base contents
  - inventory signals (有哪些表/表结构/tables/schema/字段) or nothing
    else fired → full table inventory

Multi-signal questions (e.g. "loan 和 order 表分别什么含义？有什么关系")
get all matching sections — nothing is dropped.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.i18n import L, detect_language
from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.kb.service import KbService
from trove.workflow.nodes.schema_linking import _join_hints
from trove.workflow.state import WorkflowState

_RELATIONS_RE = re.compile(r"关系|关联|血缘|来源|从哪")
_TERMS_RE = re.compile(r"口径|定义|含义|是什么意思|指标")
_KB_RE = re.compile(r"知识库|模板|示例|参考")
_INVENTORY_RE = re.compile(r"有哪些表|表结构|list\s+tables|\btables\b|\bschema\b|字段|\bcolumn\b")


def _datasource(connectors: ConnectorRegistry | None) -> str:
    if connectors is None:
        return ""
    return connectors.default_name or ""


def make_answer_metadata(
    catalog: CatalogService | None = None,
    kb: KbService | None = None,
    connectors: ConnectorRegistry | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    async def answer_metadata(state: WorkflowState) -> dict[str, Any]:
        lang = detect_language(state.question)
        q = state.question
        sections: list[str] = []

        tables = await catalog.list_tables() if catalog else []
        mentioned = [t for t in tables if t["name"].lower() in q.lower()]
        ds = _datasource(connectors)

        # 1. mentioned tables → details
        if mentioned and catalog:
            notes = {}
            if kb is not None and ds:
                notes = await kb.table_notes([t["name"] for t in mentioned], ds)
            for t in mentioned:
                lines = [L(lang, f"表 {t['name']}（{t['columns']} 列）：", f"Table {t['name']} ({t['columns']} columns):")]
                detail = await catalog.table_detail(t["name"])
                table_notes = notes.get(t["name"])
                for col in (detail or {}).get("columns", []):
                    desc = (table_notes.columns.get(col["name"]) if table_notes else None) or ""
                    line = f"  • {col['name']} ({col['type']})"
                    if desc:
                        line += f" — {desc}"
                    lines.append(line)
                if table_notes and table_notes.description:
                    lines.append(L(lang, f"  描述：{table_notes.description}", f"  Description: {table_notes.description}"))
                sections.append("\n".join(lines))

        # 2. relationships
        if _RELATIONS_RE.search(q) and connectors:
            schema = await connectors.get_schema()
            table_columns = {t.name: [c.name for c in t.columns] for t in schema.tables}
            targets = [t["name"] for t in mentioned] or list(table_columns)
            rel_lines: list[str] = []
            for table in targets:
                hints = _join_hints(table, table_columns.get(table, []), table_columns)
                if hints:
                    rel_lines.append(L(lang, f"{table} 的关联：", f"Relationships of {table}:"))
                    rel_lines.extend(f"  • {h}" for h in hints)
            if rel_lines:
                sections.append("\n".join(rel_lines))

        # 3. business terms
        if _TERMS_RE.search(q) and kb is not None and ds:
            await kb.ensure_synced(ds)
            hits = await kb.search_terms(q, ds)
            if hits:
                term_lines = [L(lang, "术语口径：", "Term definitions:")]
                for h in hits:
                    term_lines.append(f"  • {h.term} → {h.mapping}")
                    if h.definition:
                        term_lines.append(L(lang, f"    {h.definition}", f"    {h.definition}"))
                sections.append("\n".join(term_lines))

        # 4. knowledge base contents
        if _KB_RE.search(q) and kb is not None and ds:
            await kb.ensure_synced(ds)
            counts = (await kb.list_items()).get(ds, {})
            if counts:
                lines = [L(lang, f"知识库（{ds}）：", f"Knowledge base ({ds}):")]
                lines.append(L(lang, f"  • 表注释 {counts.get('table', 0)} / 术语 {counts.get('term', 0)} / 示例模板 {counts.get('example', 0) + counts.get('template', 0)} / 教训 {counts.get('lesson', 0)}", f"  • tables {counts.get('table', 0)} / terms {counts.get('term', 0)} / examples {counts.get('example', 0) + counts.get('template', 0)} / lessons {counts.get('lesson', 0)}"))
                sections.append("\n".join(lines))

        # 5. inventory (explicit signal, or nothing else fired)
        if _INVENTORY_RE.search(q) or not sections:
            if catalog:
                inv_lines = [L(lang, f"数据源共 {len(tables)} 张表：", f"The datasource has {len(tables)} tables:")]
                for t in tables:
                    inv_lines.append(f"  • {t['name']}（{t['columns']} 列）" if lang == "zh" else f"  • {t['name']} ({t['columns']} columns)")
                sections.append("\n".join(inv_lines))

        if not sections:
            return {"intent_answer": L(lang, "当前没有可用的数据源目录。", "No datasource catalog available.")}
        return {"intent_answer": "\n\n".join(sections)}

    return answer_metadata
