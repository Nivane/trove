"""Schema Linking node — identifies relevant tables for a query.

In MVP (no RAG), this node queries the datasource catalog
to find tables whose names/columns match the user's question.
The matched schema is passed downstream to gen_sql.
"""

from __future__ import annotations

from trove.core.types import NodeStatus, WorkflowContext
from trove.core.logging import get_logger
from trove.workflow.node import Node, NodeResult
from trove.workflow.node_type import NodeType

logger = get_logger(__name__)


class SchemaLinkingNode(Node):
    """Match user query to relevant database tables.

    MVP implementation: searches table/column names against
    the user query and the datasource catalog.

    Future (v0.2): uses RAG knowledge base for semantic matching.
    """

    node_type = NodeType.SCHEMA_LINKING

    def __init__(self, name: str = "schema_linking", max_tables: int = 5):
        super().__init__(name)
        self.max_tables = max_tables

    async def execute(self, ctx: WorkflowContext) -> NodeResult:
        """Identify relevant tables from the datasource catalog.

        Args:
            ctx: Workflow context with config and user message.

        Returns:
            NodeResult with matched tables in data["matched_tables"].
        """
        query = ctx.user_message.content

        try:
            # Access the catalog service from context config
            catalog = getattr(ctx.config, "_catalog_service", None)
            if catalog is None:
                # No catalog available — pass through empty schema
                return NodeResult(
                    node_name=self.name,
                    status=NodeStatus.SUCCESS,
                    data={
                        "matched_tables": [],
                        "schema_context": "No schema information available.",
                    },
                )

            # Search for relevant tables
            matches = await catalog.search_tables(query, limit=self.max_tables)

            # Build schema context for the matched tables
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
                len(matches), query[:80],
            )

            return NodeResult(
                node_name=self.name,
                status=NodeStatus.SUCCESS,
                data={
                    "matched_tables": [m["name"] for m in matches],
                    "schema_context": schema_context,
                    "match_details": matches,
                },
            )

        except Exception as e:
            logger.error("Schema linking failed: %s", e)
            return NodeResult(
                node_name=self.name,
                status=NodeStatus.ERROR,
                error=e,
                data={"schema_context": "Schema linking failed."},
            )
