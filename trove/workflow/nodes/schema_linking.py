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

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.kb.service import KbService, TableNotes, TermHit
from trove.core.logging import get_logger
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

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


def _column_line(col: dict[str, Any], notes: TableNotes | None) -> str:
    desc = notes.columns.get(col["name"]) if notes else None
    base = f"{col['name']} ({col['type']})"
    return f"{base} — {desc}" if desc else base


def _extract_value_candidates(question: str, limit: int = 5) -> list[str]:
    """Value-linking candidates from the question.

    Extracts quoted strings and capitalized/all-caps tokens (e.g.
    'Benesov', 'POPLATEK') — entities worth looking up in column values.
    Plain lowercase/Chinese questions yield nothing.
    """
    candidates: list[str] = []
    for m in _QUOTED_RE.finditer(question):
        candidates.append(m.group(1))
    for m in _CAPITALIZED_RE.finditer(question):
        candidates.append(m.group(0))
    for m in _ALL_CAPS_RE.finditer(question):
        candidates.append(m.group(0))

    seen: set[str] = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result[:limit]


async def _find_value_hits(
    connectors: ConnectorRegistry, details: list[dict[str, Any]],
    candidates: list[str],
) -> dict[str, str]:
    """Which candidates appear as actual values in matched tables' text columns.

    Returns:
        Map of value → location ("table.column").
    """
    try:
        adapter = await connectors.get()
        quote = "`" if adapter.dialect() == "mysql" else '"'
    except Exception:
        return {}

    hits: dict[str, str] = {}
    for detail in details:
        text_cols = [
            c["name"] for c in detail["columns"]
            if any(t in str(c["type"]).lower() for t in ("char", "text"))
        ][:3]
        for col in text_cols:
            for value in candidates:
                if value in hits:
                    continue
                escaped = value.replace("'", "''")
                sql = (
                    f"SELECT 1 FROM {quote}{detail['name']}{quote} "
                    f"WHERE {quote}{col}{quote} = '{escaped}' LIMIT 1"
                )
                try:
                    result = await asyncio.wait_for(
                        connectors.execute(sql), timeout=5.0,
                    )
                except Exception:
                    continue
                if result.row_count > 0:
                    hits[value] = f"{detail['name']}.{col}"
    return hits


def _join_hints(
    table_name: str, columns: list[str], table_columns: dict[str, list[str]],
) -> list[str]:
    """Infer join paths from *_id column names (works without FK metadata).

    e.g. account.district_id with a district table present →
    "account.district_id → district.district_id" (falls back to ".id").

    Args:
        table_name: The table whose columns are being inspected.
        columns: That table's column names.
        table_columns: Map of candidate target table → its column names.

    Returns:
        Join hint strings ("<table>.<col> → <target>.<target_col>").
    """
    hints = []
    for col in columns:
        if not col.endswith("_id") or len(col) <= 3:
            continue
        target = col[:-3]  # district_id → district
        if target == table_name or target not in table_columns:
            continue
        target_cols = table_columns[target]
        target_col = col if col in target_cols else ("id" if "id" in target_cols else None)
        if target_col:
            hints.append(f"{table_name}.{col} → {target}.{target_col}")
    return hints


def make_schema_linking(
    catalog: CatalogService | None = None,
    max_tables: int = 5,
    kb: KbService | None = None,
    connectors: ConnectorRegistry | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the schema_linking node bound to catalog and knowledge base.

    Args:
        catalog: Metadata catalog for table search (None → pass through empty).
        max_tables: Maximum tables to match per query.
        kb: Optional knowledge base for term matching and annotations.
        connectors: Registry providing the active datasource name (KB scope).
            KB is only consulted when a datasource context exists.

    Returns:
        Async node function taking WorkflowState and returning a partial update.
    """

    async def schema_linking(state: WorkflowState) -> dict[str, Any]:
        # Upstream node failed — pass through without running
        if state.error:
            return {}

        # Knowledge base is scoped to the active datasource
        datasource = connectors.default_name if connectors is not None else ""

        # 1. Knowledge base term matching (substring, works for Chinese)
        term_hits: list[TermHit] = []
        if kb is not None and datasource:
            await kb.ensure_synced(default_datasource=datasource)
            term_hits = await kb.search_terms(state.question, datasource)

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
            notes = (
                await kb.table_notes(matched_names, datasource)
                if (kb is not None and datasource) else {}
            )
            details = []
            table_columns: dict[str, list[str]] = {}
            for name in matched_names:
                detail = await catalog.table_detail(name)
                if detail is None:
                    continue
                details.append(detail)
                table_columns[detail["name"]] = [c["name"] for c in detail["columns"]]

            schema_parts = []
            for detail in details:
                name = detail["name"]
                table_notes = notes.get(name)
                cols = ", ".join(
                    _column_line(c, table_notes) for c in detail["columns"]
                )
                parts = [
                    f"Table: {name}\n"
                    f"Columns: {cols}\n"
                    f"Approximate rows: {detail.get('row_count', 'unknown')}\n",
                ]
                hints = _join_hints(
                    name, [c["name"] for c in detail["columns"]], table_columns,
                )
                if hints:
                    parts.append("Join hints: " + ", ".join(hints) + "\n")
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

            # 4. Value linking: question entities found in column values
            if connectors is not None:
                candidates = _extract_value_candidates(state.question)
                if candidates:
                    value_hits = await _find_value_hits(connectors, details, candidates)
                    if value_hits:
                        hint_lines = "\n".join(
                            f"Value hints: '{v}' found in {loc}"
                            for v, loc in value_hits.items()
                        )
                        schema_context += "\n\n" + hint_lines

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
