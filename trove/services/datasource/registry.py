"""Connector registry — manages all registered database adapters.

Provides a central point for registering, discovering,
and switching between database connections.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import replace
from typing import Any

from trove.core.types import DatasourceConfig, QueryResult, SchemaInfo
from trove.core.errors import DatasourceConflictError, DatasourceError
from trove.core.logging import get_logger
from trove.core.metrics import record_sql, record_sql_cache_hit
from trove.services.datasource.adapters.base import DatabaseAdapter
from trove.services.datasource.adapters.sqlite import SQLiteAdapter
from trove.services.datasource.adapters.mysql import MySQLAdapter
from trove.services.datasource.adapters.doris import DorisAdapter
from trove.services.datasource.adapters.postgres import PostgresAdapter
from trove.services.datasource.adapters.clickhouse import ClickHouseAdapter
from trove.services.datasource.adapters.duckdb import DuckDBAdapter
from trove.services.datasource.naming import backfill_ds_id, is_path_safe

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
    "doris": DorisAdapter,
    "postgres": PostgresAdapter,
    "clickhouse": ClickHouseAdapter,
    "duckdb": DuckDBAdapter,
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
    """Central registry for all active database connections.

    Read-only result caching: probe_query / check_result / execute_sql
    repeatedly run the same SELECT within and across questions (drafts,
    verification, follow-ups). execute() keeps a bounded short-TTL cache
    keyed on (datasource, normalized SQL) so identical reads skip the
    database. Writes never enter the cache (execute_unsafe bypasses it),
    so cached entries only ever mirror immutable query results.
    """

    def __init__(
        self,
        result_cache_ttl_s: float = 60.0,
        result_cache_max_entries: int = 256,
    ):
        self._adapters: dict[str, DatabaseAdapter] = {}
        self._default_name: str | None = None
        # name → immutable ds_id (identity map; conflict guard source).
        self._ds_ids: dict[str, str] = {}
        # Display-safe connection info per datasource (no credentials).
        self._datasource_info: dict[str, dict[str, Any]] = {}
        # (datasource, normalized SQL) → (stored_at, QueryResult)
        self._result_cache: dict[tuple, tuple[float, QueryResult]] = {}
        self._result_cache_ttl_s = result_cache_ttl_s
        self._result_cache_max = result_cache_max_entries
        self._result_cache_hits = 0

    # ── Connection management ────────────────────────────

    async def register(
        self,
        config: DatasourceConfig,
        set_default: bool = False,
    ) -> DatabaseAdapter:
        """Register and connect to a datasource.

        Args:
            config: Datasource configuration. An empty ``ds_id`` is
                backfilled with a fresh UUID before any state is written.
            set_default: Make this the default datasource.

        Returns:
            The connected DatabaseAdapter instance.

        Raises:
            DatasourceError: If the datasource type is unsupported or
                connection fails, or the name is not path-safe.
            DatasourceConflictError: A different datasource identity is
                already registered under the same name (409 at the API).

        Note:
            Registration is idempotent for the *same* ds_id under the
            same name (reconnect / re-register). A *different* ds_id
            under an existing name is a conflict, never a silent
            overwrite.
        """
        config = self._ensure_identity(config)
        adapter = await self.prepare(config)
        prev = self._adapters.get(config.name)
        try:
            self._activate(config, adapter, set_default)
        except BaseException:
            await adapter.disconnect()
            raise
        if prev is not None and prev is not adapter:
            # 同身份幂等覆盖:断开被替换的旧连接,避免泄漏。
            await prev.disconnect()
        return adapter

    def _ensure_identity(self, config: DatasourceConfig) -> DatasourceConfig:
        """Backfill a stable ds_id and reject path-unsafe names.

        Deterministic ``uuid5(type:name)`` keeps the identity immutable
        across re-registrations (reconnect, demo re-setup, boot restore)
        — an empty ds_id means "same datasource", not "new creation".
        Brand-new identities are generated by the admin flow up front.
        """
        if not is_path_safe(config.name):
            raise DatasourceError(
                message=f"unsafe datasource name {config.name!r}",
                datasource=config.name,
            )
        if not config.ds_id:
            config = replace(config, ds_id=backfill_ds_id(config.type, config.name))
        return config

    def ensure_identity(self, config: DatasourceConfig) -> DatasourceConfig:
        """Public identity hygiene for the transactional admin register path."""
        return self._ensure_identity(config)

    def activate(
        self, config: DatasourceConfig, adapter: DatabaseAdapter, set_default: bool
    ) -> None:
        """Insert a prepared (connected) adapter into the registry.

        The final step of the transactional registration sequence
        (connect → persist → activate); documented as public for the
        admin surface and distinct from ``register`` which runs the whole
        sequence itself.
        """
        self._activate(self._ensure_identity(config), adapter, set_default)

    def identity_of(self, name: str) -> str | None:
        """The immutable ds_id currently registered under ``name``."""
        return self._ds_ids.get(name)

    async def prepare(self, config: DatasourceConfig) -> DatabaseAdapter:
        """Connect an adapter WITHOUT inserting it into the registry.

        Enables the transactional registration ordering (connect →
        persist → activate) used by the admin surface: callers own the
        persistence step between ``prepare`` and ``activate`` and are
        responsible for ``adapter.disconnect()`` on rollback.
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
        return adapter

    def _activate(
        self, config: DatasourceConfig, adapter: DatabaseAdapter, set_default: bool
    ) -> None:
        """Insert a connected adapter into the registry (conflict-guarded)."""
        config = self._ensure_identity(config)
        existing = self._ds_ids.get(config.name)
        if existing is not None and existing != config.ds_id:
            raise DatasourceConflictError(
                message=(
                    f"datasource '{config.name}' already exists with a "
                    "different identity; refusing to overwrite"
                ),
                datasource=config.name,
            )
        self._adapters[config.name] = adapter
        self._ds_ids[config.name] = config.ds_id
        self._datasource_info[config.name] = {
            "id": config.ds_id,
            "type": config.type,
            "connection": _sanitize_connection(config.connection_params),
        }

        if set_default or config.default or self._default_name is None:
            self._default_name = config.name

        logger.info(
            "Registered datasource '%s' (id=%s, type=%s, default=%s)",
            config.name, config.ds_id, config.type, self._default_name == config.name,
        )

    async def unregister(self, name: str) -> None:
        """Disconnect and remove a datasource.

        Args:
            name: The datasource name to remove.
        """
        if name not in self._adapters:
            return

        adapter = self._adapters.pop(name)
        self._ds_ids.pop(name, None)
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
        adapter = await self.get(datasource)
        # 测试替身等轻量 adapter 可能缺 dialect — getattr 防御
        self._guard_read_only(sql, getattr(adapter, "dialect", lambda: "")())
        key = (
            adapter.name,
            re.sub(r"\s+", " ", sql.strip()).upper(),
        )
        hit = self._result_cache.get(key)
        now = time.monotonic()
        if hit is not None and now - hit[0] < self._result_cache_ttl_s:
            self._result_cache_hits += 1
            cached = hit[1]
            record_sql_cache_hit(adapter.name)
            return replace(
                cached, rows=list(cached.rows), columns=list(cached.columns),
            )
        # 指标:每次真实执行计一条(cancelled = 客户端中止链路,error = 驱动失败)。
        # 失败也计数——生产上"error 率"是健康度的核心信号。
        started = time.monotonic()
        try:
            result = await adapter.execute(sql)
            record_sql(adapter.name, "success", time.monotonic() - started)
        except asyncio.CancelledError:
            record_sql(adapter.name, "cancelled", time.monotonic() - started)
            raise
        except Exception:
            record_sql(adapter.name, "error", time.monotonic() - started)
            raise
        self._put_result_cache(key, result)
        return result

    def _put_result_cache(self, key: tuple, result: QueryResult) -> None:
        """写入结果缓存(短 TTL + 容量上限,超限淘汰最旧条目)。"""
        if self._result_cache_ttl_s <= 0:
            return
        self._result_cache[key] = (time.monotonic(), result)
        if len(self._result_cache) > self._result_cache_max:
            oldest = min(
                self._result_cache, key=lambda k: self._result_cache[k][0],
            )
            self._result_cache.pop(oldest, None)

    def result_cache_stats(self) -> dict[str, int]:
        """缓存命中统计(诊断/测试):当前条目数 + 累计命中数。"""
        return {
            "entries": len(self._result_cache),
            "hits": self._result_cache_hits,
            "max_entries": self._result_cache_max,
        }

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

    async def explain(self, sql: str, datasource: str | None = None) -> QueryResult:
        """Fetch the execution plan for a SELECT (EXPLAIN).

        Read-only: the inner statement passes the same guard as ``execute``
        (only SELECT reaches the adapter — the EXPLAIN prefix itself is
        applied here, not in the adapter). Milliseconds, no row data.
        Deliberately bypasses the result cache (plans are cheap and the
        key space is the same as execute's).
        """
        adapter = await self.get(datasource)
        self._guard_read_only(sql, adapter.dialect())
        prefix = "EXPLAIN QUERY PLAN" if adapter.dialect() == "sqlite" else "EXPLAIN"
        return await adapter.execute(f"{prefix} {sql}")

    @staticmethod
    def _guard_read_only(sql: str, dialect: str = "") -> None:
        """Reject non-SELECT statements (AST-level read-only firewall).

        统一执行入口的只读门:execute / explain 共用。AST 整树检查,
        覆盖关键词正则与「只查顶层」都绕不过去的手法——data-modifying
        CTE(WITH x AS (DELETE ...) SELECT)、注释拆分 DEL/**/ETE、
        危险函数(SLEEP/LOAD_FILE)、元数据表侦察(sqlite_master 等)。
        """
        from trove.services.sql.guard import check_readonly

        ok, reasons = check_readonly(sql, dialect)
        if not ok:
            raise DatasourceError(
                message="Trove is read-only: " + "; ".join(reasons[:3])
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
