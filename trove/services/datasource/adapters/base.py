"""Abstract base class for all database adapters.

Each database type (SQLite, PostgreSQL, DuckDB, etc.)
implements this interface so the rest of the system
can interact with any database uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from trove.core.types import Capabilities, QueryResult, SchemaInfo


class DatabaseAdapter(ABC):
    """Uniform interface for all database connectors.

    Subclasses must implement all abstract methods.
    Each adapter is responsible for:
      - Connection lifecycle (connect/disconnect)
      - Query execution
      - Schema introspection
      - Capability reporting
    """

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the database."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the database connection."""
        ...

    @abstractmethod
    async def execute(self, sql: str) -> QueryResult:
        """Execute a SQL query and return results.

        Args:
            sql: The SQL statement to execute (SELECT only for safety).

        Returns:
            QueryResult with columns, rows, and execution metadata.
        """
        ...

    async def interrupt(self) -> None:
        """Best-effort cancellation of the in-flight query (no-op default).

        Adapters whose driver exposes a cross-task cancel (sqlite3
        interrupt, psycopg cancel, MySQL KILL QUERY, duckdb interrupt)
        override this so a client abort stops the database work, not
        just the awaiting coroutine. Implementations must be bounded
        and must never raise — the caller is already unwinding a
        cancellation.
        """
        return None

    @abstractmethod
    async def get_schema(self) -> SchemaInfo:
        """Introspect the database and return full schema metadata."""
        ...

    @abstractmethod
    async def get_capabilities(self) -> Capabilities:
        """Report database capabilities (CTE, window functions, etc.)."""
        ...

    @staticmethod
    @abstractmethod
    def dialect() -> str:
        """Return the SQL dialect name for this database type.

        Examples: "sqlite", "postgres", "mysql", "snowflake".
        """
        ...

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args: Any):
        await self.disconnect()
