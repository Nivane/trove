"""Connector registry — manages all registered database adapters.

Provides a central point for registering, discovering,
and switching between database connections.
"""

from __future__ import annotations

import re
from typing import Any

from trove.core.types import DatasourceConfig, QueryResult, SchemaInfo
from trove.core.errors import DatasourceError
from trove.core.logging import get_logger
from trove.services.datasource.adapters.base import DatabaseAdapter
from trove.services.datasource.adapters.sqlite import SQLiteAdapter
from trove.services.datasource.adapters.mysql import MySQLAdapter
from trove.services.datasource.adapters.clickhouse import ClickHouseAdapter
from trove.services.datasource.adapters.duckdb import DuckDBAdapter

logger = get_logger(__name__)

# ── Adapter factory mapping ──────────────────────────────
# Adapter modules import their drivers lazily, so importing them here
# never requires the optional extras to be installed.

# Keys that look like credentials — never exposed to the Web UI.
_SENSITIVE_KEY_RE = re.compile(r"password|passwd|secret|token|credential", re.IGNORECASE)


def _sanitize_connection(params: dict[str, Any]) -> dict[str, Any]:
    """Strip credential-like keys from connection params for display.

    connection_params may carry password keys when parsed from a URL
    (see urls.py), so filtering by key name is the safety boundary —
    only non-secret fields (host/port/database/path/user) survive.
    """
    return {k: v for k, v in params.items() if not _SENSITIVE_KEY_RE.search(k)}


_ADAPTER_REGISTRY: dict[str, type[DatabaseAdapter]] = {
    "sqlite": SQLiteAdapter,
    "mysql": MySQLAdapter,
    "clickhouse": ClickHouseAdapter,
    "duckdb": DuckDBAdapter,
    # "postgres": PostgresAdapter,
    # "snowflake": SnowflakeAdapter,
}


def register_adapter(dialect: str, adapter_cls: type[DatabaseAdapter]) -> None:
    """Register a new database adapter class for a dialect.

    Args:
        dialect: The SQL dialect name (e.g. "postgres").
        adapter_cls: The adapter class implementing DatabaseAdapter.
    """
    _ADAPTER_REGISTRY[dialect] = adapter_cls
    logger.info("Registered database adapter: %s → %s", dialect, adapter_cls.__name__)


class ConnectorRegistry:
    """Central registry for all active database connections."""

    def __init__(self):
        self._adapters: dict[str, DatabaseAdapter] = {}
        self._default_name: str | None = None
        # Display-safe connection info per datasource (no credentials).
        self._datasource_info: dict[str, dict[str, Any]] = {}

    # ── Connection management ────────────────────────────

    async def register(
        self,
        config: DatasourceConfig,
        set_default: bool = False,
    ) -> DatabaseAdapter:
        """Register and connect to a datasource.

        Args:
            config: Datasource configuration.
            set_default: Make this the default datasource.

        Returns:
            The connected DatabaseAdapter instance.

        Raises:
            DatasourceError: If the datasource type is unsupported or connection fails.
        """
        adapter_cls = _ADAPTER_REGISTRY.get(config.type)
        if adapter_cls is None:
            raise DatasourceError(
                message=f"Unsupported datasource type: {config.type}. "
                        f"Available: {list(_ADAPTER_REGISTRY.keys())}",
                datasource=config.name,
            )

        adapter = adapter_cls(
            name=config.name,
            config={**config.connection_params, **config.credentials},
        )
        await adapter.connect()
        self._adapters[config.name] = adapter
        self._datasource_info[config.name] = {
            "type": config.type,
            "connection": _sanitize_connection(config.connection_params),
        }

        if set_default or config.default or self._default_name is None:
            self._default_name = config.name

        logger.info(
            "Registered datasource '%s' (type=%s, default=%s)",
            config.name, config.type, self._default_name == config.name,
        )
        return adapter

    async def unregister(self, name: str) -> None:
        """Disconnect and remove a datasource.

        Args:
            name: The datasource name to remove.
        """
        if name not in self._adapters:
            return

        adapter = self._adapters.pop(name)
        self._datasource_info.pop(name, None)
        await adapter.disconnect()

        if self._default_name == name:
            self._default_name = next(iter(self._adapters), None)

        logger.info("Unregistered datasource '%s'", name)

    async def get(self, name: str | None = None) -> DatabaseAdapter:
        """Get a connected adapter by name (or the default).

        Args:
            name: Datasource name. If None, returns the default.

        Returns:
            The DatabaseAdapter instance.

        Raises:
            DatasourceError: If no adapter matches or none are registered.
        """
        if not self._adapters:
            raise DatasourceError(
                message="No datasources registered. Use /datasource to add one.",
                datasource="",
            )

        target = name or self._default_name
        if target is None or target not in self._adapters:
            available = list(self._adapters.keys())
            raise DatasourceError(
                message=f"Datasource '{target}' not found. Available: {available}",
                datasource=target or "",
            )

        return self._adapters[target]

    # ── Querying ─────────────────────────────────────────

    async def execute(self, sql: str, datasource: str | None = None) -> QueryResult:
        """Execute SQL on a specific datasource.

        Read-only guard: only SELECT statements reach the adapter —
        DML/DDL (insert/update/delete/create/drop/...) is rejected here.
        This is the execution-layer write protection backing the intent
        layer's write refusal; it covers every pipeline entry point
        (execute_sql, probe_query, check_result), so a write slipping
        past intent classification can never touch the datasource.

        Args:
            sql: The SQL to execute.
            datasource: Target datasource name (default if None).

        Returns:
            QueryResult.

        Raises:
            DatasourceError: If the SQL is not a SELECT statement.
        """
        self._guard_read_only(sql)
        adapter = await self.get(datasource)
        return await adapter.execute(sql)

    async def execute_unsafe(
        self, sql: str, datasource: str | None = None
    ) -> QueryResult:
        """Execute SQL bypassing the read-only guard.

        Escape hatch for explicit write-permission paths (SQLExecutor
        DANGEROUS mode, admin tooling). The query pipeline must never
        use this — writes only happen here when the user opted into
        `permission dangerous`.
        """
        adapter = await self.get(datasource)
        return await adapter.execute(sql)

    @staticmethod
    def _guard_read_only(sql: str) -> None:
        """Reject non-SELECT statements (best-effort; sqlglot optional)."""
        try:
            import sqlglot
            from sqlglot import exp
        except ImportError:
            logger.warning("sqlglot not available; skipping read-only guard")
            return
        statements: list | None = None
        # 反引号标识符(MySQL 方言,KB 探测等内部 SQL 使用)默认解析失败 →
        # 回退 mysql 方言再试;两者都失败才拒绝(安全方向)
        for dialect in (None, "mysql"):
            try:
                statements = sqlglot.parse(sql, dialect=dialect)
                break
            except Exception:
                continue
        if statements is None:
            raise DatasourceError(
                message="SQL rejected: could not parse statement"
            )
        for stmt in statements:
            if stmt is None:
                continue
            if not isinstance(stmt, exp.Select):
                raise DatasourceError(
                    message=(
                        "Trove is read-only: only SELECT queries are allowed "
                        f"(rejected: {stmt.sql()[:120]})"
                    )
                )

    async def get_schema(self, datasource: str | None = None) -> SchemaInfo:
        """Get schema info for a datasource."""
        adapter = await self.get(datasource)
        return await adapter.get_schema()

    # ── Info ─────────────────────────────────────────────

    @property
    def default_name(self) -> str | None:
        return self._default_name

    def list_names(self) -> list[str]:
        """List all registered datasource names."""
        return list(self._adapters.keys())

    def list_info(self) -> list[dict[str, Any]]:
        """List registered datasources with display-safe connection info.

        Each entry carries name/default plus the sanitized type and
        connection params recorded at registration time (credentials
        are never stored here — see _sanitize_connection).
        """
        return [
            {
                "name": name,
                "default": name == self._default_name,
                **self._datasource_info.get(name, {}),
            }
            for name in self._adapters
        ]

    def is_registered(self, name: str) -> bool:
        return name in self._adapters

    async def close_all(self) -> None:
        """Disconnect all registered datasources."""
        for name in list(self._adapters.keys()):
            await self.unregister(name)
