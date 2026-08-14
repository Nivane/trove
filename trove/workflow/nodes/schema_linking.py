"""Schema Linking node — identifies relevant tables for a query.

Two matching sources:
  1. Knowledge base terms: business terms (中文子串/alias 匹配) whose
     tables join the match set — this is what makes Chinese questions work
  2. Datasource catalog: table/column name search (ASCII tokens)

Matched tables get their human annotations (table description, column
descriptions, metric definitions) merged into the schema context.

Node shape: `async def schema_linking(state: WorkflowState) -> dict`
returns a partial state update.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trove.services.datasource.catalog import CatalogService
from trove.services.kb.service import KbService, TableNotes, TermHit
from trove.core.logging import get_logger
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)


def _dedup_tables(hits: list[TermHit]) -> list[str]:
    """Flatten term table lists, preserving order and dropping duplicates."""
    names: list[str] = []
    for hit in hits:
        for table in hit.tables:
            if table not in names:
                names.append(table)
    return names


def _column_line(col: dict[str, Any], notes: TableNotes | None) -> str:
    desc = notes.columns.get(col["name"]) if notes else None
    base = f"{col['name']} ({col['type']})"
    return f"{base} — {desc}" if desc else base


def make_schema_linking(
    catalog: CatalogService | None = None,
    max_tables: int = 5,
    kb: KbService | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the schema_linking node bound to catalog and knowledge base.

    Args:
        catalog: Metadata catalog for table search (None → pass through empty).
        max_tables: Maximum tables to match per query.
        kb: Optional knowledge base for term matching and annotations.

    Returns:
        Async node function taking WorkflowState and returning a partial update.
    """

    async def schema_linking(state: WorkflowState) -> dict[str, Any]:
        # Upstream node failed — pass through without running
        if state.error:
            return {}

        # 1. Knowledge base term matching (substring, works for Chinese)
        term_hits: list[TermHit] = []
        if kb is not None:
            await kb.ensure_synced()
            term_hits = await kb.search_terms(state.question)

        update: dict[str, Any]

        if catalog is None:
            update = {
                "matched_tables": _dedup_tables(term_hits),
                "schema_context": "No schema information available.",
            }
        else:
            # 2. Catalog table search (existing behavior)
            try:
                matches = await catalog.search_tables(state.question, limit=max_tables)
            except Exception as e:
                logger.error("Schema linking failed: %s", e)
                return {"error": f"Schema linking failed: {e}"}

            # Term tables join the match set (catalog order first)
            matched_names = [m["name"] for m in matches]
            for table in _dedup_tables(term_hits):
                if table not in matched_names:
                    matched_names.append(table)

            # 3. Human annotations merged into the schema context
            notes = await kb.table_notes(matched_names) if kb is not None else {}
            schema_parts = []
            for name in matched_names:
                detail = await catalog.table_detail(name)
                if detail is None:
                    continue
                table_notes = notes.get(name)
                cols = ", ".join(
                    _column_line(c, table_notes) for c in detail["columns"]
                )
                parts = [
                    f"Table: {detail['name']}\n"
                    f"Columns: {cols}\n"
                    f"Approximate rows: {detail.get('row_count', 'unknown')}\n",
                ]
                if table_notes and table_notes.description:
                    parts.append(f"Description: {table_notes.description}\n")
                if table_notes and table_notes.metrics:
                    parts.append(
                        "Metrics:\n"
                        + "".join(f"- {m} — {d}\n" for m, d in table_notes.metrics.items())
                    )
                schema_parts.append("".join(parts))

            schema_context = "\n".join(schema_parts) if schema_parts else (
                "No matching tables found. Consider using /tables to list available tables."
            )

            logger.debug(
                "Schema linking matched %d tables for query: %s",
                len(matched_names), state.question[:80],
            )

            update = {
                "matched_tables": matched_names,
                "schema_context": schema_context,
            }

        if term_hits:
            update["kb_hits"] = [
                {
                    "kind": "term",
                    "term": h.term,
                    "mapping": h.mapping,
                    "definition": h.definition,
                    "tables": h.tables,
                }
                for h in term_hits
            ]
        return update

    return schema_linking
