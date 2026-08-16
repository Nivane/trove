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

# 停用词:子串匹配会把 "to" 命中 bank_to/account_to、"an" 命中 balance 等,
# 产生假阳性表匹配(实测 BIRD 题 "…statements to be issued" 误中 order 表)。
_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "for", "nor", "so", "yet",
    "to", "of", "in", "on", "at", "by", "with", "from", "into", "over",
    "is", "are", "was", "were", "be", "been", "being", "am", "do", "does",
    "did", "has", "have", "had", "will", "would", "can", "could", "shall",
    "should", "may", "might", "must", "not", "no", "if", "then", "than",
    "that", "this", "these", "those", "it", "its", "as", "we", "you",
    "they", "them", "their", "he", "she", "him", "her", "his", "who",
    "whom", "which", "what", "when", "where", "how", "why", "all", "any",
    "each", "both", "few", "more", "most", "some", "such", "there",
    "also", "only", "very", "just", "about", "between", "during",
    "because", "while", "after", "before", "until", "above", "below",
    "per", "via", "due", "out", "up", "down", "off",
}


def _token_variants(token: str) -> list[str]:
    """轻量词形归一:复数/过去式也参与子串匹配。

    "clients"→"client"、"issued"→"issue" 才能命中表/列名;不改变子串
    语义(变体只是额外的候选,原 token 始终保留)。
    """
    variants = [token]
    if token.endswith("ies") and len(token) > 4:
        variants.append(token[:-3] + "y")
    elif token.endswith("s") and len(token) > 3:
        variants.append(token[:-1])
    if token.endswith("ed") and len(token) > 4:
        variants.append(token[:-1])
    return variants


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
        # of a table/column name). Stopwords are dropped ("to" would
        # spuriously hit bank_to/account_to) and plural/past forms are
        # normalized ("clients" → "client").
        tokens = {
            t.lower() for t in re.findall(r"\w+", query)
            if len(t) >= 2 and t.lower() not in _STOPWORDS
        }
        variants: set[str] = set()
        for tok in tokens:
            variants.update(_token_variants(tok))

        results = []
        for table in schema.tables:
            # Match on table name or column names
            name_match = any(v in table.name.lower() for v in variants)
            col_match = any(
                v in c.name.lower()
                for c in table.columns
                for v in variants
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
