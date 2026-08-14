"""Schema Linking node — identifies relevant tables for a query.

In MVP (no RAG), this node queries the datasource catalog
to find tables whose names/columns match the user's question.
The matched schema is passed downstream to gen_sql.

Node shape: `async def schema_linking(state: WorkflowState) -> dict`
returns a partial state update.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trove.services.datasource.catalog import CatalogService
from trove.core.logging import get_logger
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)


def make_schema_linking(
    catalog: CatalogService | None = None,
    max_tables: int = 5,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the schema_linking node bound to a catalog service.

    Args:
        catalog: Metadata catalog for table search (None → pass through empty).
        max_tables: Maximum tables to match per query.

    Returns:
        Async node function taking WorkflowState and returning a partial update.
    """

    async def schema_linking(state: WorkflowState) -> dict[str, Any]:
        # Upstream node failed — pass through without running
        if state.error:
            return {}

        if catalog is None:
            return {
                "matched_tables": [],
                "schema_context": "No schema information available.",
            }

        try:
            matches = await catalog.search_tables(state.question, limit=max_tables)

            schema_parts = []
            for match in matches:
                detail = await catalog.table_detail(match["name"])
                if detail is None:
                    continue
                cols = ", ".join(
                    f"{c['name']} ({c['type']})" for c in detail["columns"]
                )
                schema_parts.append(
                    f"Table: {detail['name']}\n"
                    f"Columns: {cols}\n"
                    f"Approximate rows: {detail.get('row_count', 'unknown')}\n"
                )

            schema_context = "\n".join(schema_parts) if schema_parts else (
                "No matching tables found. Consider using /tables to list available tables."
            )

            logger.debug(
                "Schema linking matched %d tables for query: %s",
                len(matches), state.question[:80],
            )

            return {
                "matched_tables": [m["name"] for m in matches],
                "schema_context": schema_context,
            }

        except Exception as e:
            logger.error("Schema linking failed: %s", e)
            return {"error": f"Schema linking failed: {e}"}

    return schema_linking
