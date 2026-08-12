"""Metadata catalog — browse datasource schema and tables.

Provides both physical browsing (tables/schemas/columns)
and search across registered datasources.
"""

from __future__ import annotations

import re
from typing import Any

from trove.core.types import SchemaInfo, TableInfo
from trove.core.logging import get_logger
from trove.services.datasource.registry import ConnectorRegistry

logger = get_logger(__name__)


class CatalogService:
    """Browsing and search service for database metadata."""

    def __init__(self, registry: ConnectorRegistry):
        self._registry = registry

    async def list_tables(
        self,
        datasource: str | None = None,
        schema_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all tables in a datasource.

        Args:
            datasource: Target datasource (default if None).
            schema_filter: Optional schema name pattern.

        Returns:
            List of table summaries.
        """
        schema = await self._registry.get_schema(datasource)
        tables = schema.tables

        if schema_filter:
            tables = [t for t in tables if schema_filter.lower() in t.schema.lower()]

        return [
            {
                "name": t.name,
                "schema": t.schema,
                "columns": len(t.columns),
                "row_count": t.row_count_estimate,
            }
            for t in tables
        ]

    async def table_detail(
        self,
        table_name: str,
        datasource: str | None = None,
    ) -> dict[str, Any] | None:
        """Get detailed metadata for a specific table.

        Args:
            table_name: Name of the table.
            datasource: Target datasource.

        Returns:
            Table detail dict or None if not found.
        """
        schema = await self._registry.get_schema(datasource)
        for table in schema.tables:
            if table.name.lower() == table_name.lower():
                return {
                    "name": table.name,
                    "schema": table.schema,
                    "row_count": table.row_count_estimate,
                    "columns": [
                        {
                            "name": c.name,
                            "type": c.type,
                            "nullable": c.nullable,
                            "primary_key": c.primary_key,
                            "foreign_key": c.foreign_key,
                        }
                        for c in table.columns
                    ],
                }
        return None

    async def table_columns(
        self,
        table_name: str,
        datasource: str | None = None,
    ) -> list[dict[str, Any]]:
        """List columns for a specific table.

        Args:
            table_name: Name of the table.
            datasource: Target datasource.

        Returns:
            List of column info dicts.
        """
        detail = await self.table_detail(table_name, datasource)
        if detail is None:
            return []
        return detail["columns"]

    async def search_tables(
        self,
        query: str,
        datasource: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Fuzzy search for tables by name.

        Args:
            query: Search query string.
            datasource: Target datasource.
            limit: Maximum results.

        Returns:
            List of matching table summaries.
        """
        schema = await self._registry.get_schema(datasource)

        # Tokenize the query and match individual words against table and
        # column names (the whole query string almost never is a substring
        # of a table/column name).
        tokens = {t.lower() for t in re.findall(r"\w+", query) if len(t) >= 2}

        results = []
        for table in schema.tables:
            # Match on table name or column names
            name_match = any(tok in table.name.lower() for tok in tokens)
            col_match = any(
                tok in c.name.lower()
                for c in table.columns
                for tok in tokens
            )

            if name_match or col_match:
                results.append({
                    "name": table.name,
                    "schema": table.schema,
                    "columns": len(table.columns),
                    "row_count": table.row_count_estimate,
                    "match_type": "name" if name_match else "column",
                })

        # Sort: name matches first, then column matches
        results.sort(key=lambda r: (0 if r["match_type"] == "name" else 1, r["name"]))
        return results[:limit]

    async def get_schema_ddl(
        self,
        table_name: str,
        datasource: str | None = None,
    ) -> str:
        """Generate CREATE TABLE DDL from schema metadata.

        Args:
            table_name: Name of the table.
            datasource: Target datasource.

        Returns:
            DDL string representing the table structure.
        """
        detail = await self.table_detail(table_name, datasource)
        if detail is None:
            return f"-- Table '{table_name}' not found"

        cols = []
        for col in detail["columns"]:
            nullable = "" if col["nullable"] else " NOT NULL"
            pk = " PRIMARY KEY" if col["primary_key"] else ""
            cols.append(f"  {col['name']} {col['type']}{nullable}{pk}")

        return f"CREATE TABLE {table_name} (\n" + ",\n".join(cols) + "\n);"
