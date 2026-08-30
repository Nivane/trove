"""Lineage service — deterministic data lineage from SQL definitions & query history.

Store layout (mirrors the KB service):
  source of truth    .trove/lineage/<datasource>/definitions.yml
                       (CREATE VIEW / CREATE TABLE AS / reference queries —
                        human-authored ETL material, optional)
  runtime mirror     .trove/lineage/lineage.sqlite
                       lineage_definitions — producers (named DDL/view/query)
                       lineage_query_log   — executed query history (deduped)
  runtime capture    record_query() hooks the execute_sql node so every
                       answered question accumulates as downstream lineage.

Retrieval is deterministic (parse facts only, no LLM):
  table_upstream(T)      definitions producing T (name == T)
  table_downstream(T)    definitions + recorded queries reading T
  column_lineage(T, c)   producers of T.c + consumers touching (T, c)

YAML is synced lazily by mtime, mirroring KbService.ensure_synced.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import yaml

from trove.core.logging import get_logger
from trove.services.lineage.parse import (
    QueryDigest,
    analyze_query,
    normalization_key,
)

logger = get_logger(__name__)

LINEAGE_DIR_NAME = "lineage"

_CREATE_DEFS = """CREATE TABLE IF NOT EXISTS lineage_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    sql TEXT NOT NULL,
    dialect TEXT NOT NULL DEFAULT 'sqlite',
    digest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(datasource, name)
)"""

_CREATE_LOG = """CREATE TABLE IF NOT EXISTS lineage_query_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource TEXT NOT NULL,
    shard TEXT NOT NULL,
    sql TEXT NOT NULL,
    dialect TEXT NOT NULL DEFAULT 'sqlite',
    digest_json TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    runs INTEGER NOT NULL DEFAULT 1,
    UNIQUE(datasource, shard)
)"""

_CREATE_SYNC = """CREATE TABLE IF NOT EXISTS lineage_sync (
    file_path TEXT PRIMARY KEY,
    mtime REAL NOT NULL
)"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest_json(digest: QueryDigest | None) -> str:
    if digest is None:
        return "null"
    return json.dumps(asdict(digest), ensure_ascii=False)


def _tables_of(digest_json: str) -> list[str]:
    data = json.loads(digest_json or "null")
    return (data or {}).get("tables_read", []) or []


def _columns_of(digest_json: str) -> list[list[str]]:
    data = json.loads(digest_json or "null")
    return [list(c) for c in ((data or {}).get("columns_read", []) or [])]


def _outputs_of(digest_json: str) -> list[dict[str, Any]]:
    data = json.loads(digest_json or "null")
    out = []
    for name, sources in ((data or {}).get("outputs", []) or []):
        out.append({
            "name": name,
            "sources": [
                {"table": s.get("table", ""), "column": s.get("column", ""), "expr": s.get("expr", "")}
                for s in (sources or [])
            ],
        })
    return out


class LineageService:
    """Per-project lineage store: definitions + executed-query history."""

    def __init__(self, project_root: str | Path, lineage_dir: str | Path | None = None):
        self.root = Path(project_root)
        self.lineage_dir = (
            Path(lineage_dir) if lineage_dir is not None
            else self.root / ".trove" / LINEAGE_DIR_NAME
        )
        self.db_path = self.lineage_dir / "lineage.sqlite"

    def definitions_yaml(self, datasource: str) -> Path:
        return self.lineage_dir / datasource / "definitions.yml"

    async def _conn(self) -> aiosqlite.Connection:
        self.lineage_dir.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self.db_path))
        await conn.execute(_CREATE_DEFS)
        await conn.execute(_CREATE_LOG)
        await conn.execute(_CREATE_SYNC)
        await conn.commit()
        return conn

    # ── Ingest ────────────────────────────────────────────

    async def ingest_definition(
        self,
        sql: str,
        datasource: str,
        name: str | None = None,
        dialect: str = "sqlite",
    ) -> bool:
        """Register one ETL/view/query definition. Returns False if unusable.

        CREATE VIEW / CREATE TABLE AS SELECT → lineage_definitions producer
        (name from DDL when not given). Plain queries → query_log consumer.
        Unparseable SQL → ignored (never blocks ingestion).
        """
        digest = analyze_query(sql, dialect)
        if digest is None:
            logger.info("lineage: unparseable definition ignored for %s", datasource)
            return False
        if digest.kind in ("create_view", "create_table_as") and digest.name:
            ddl_name = name or digest.name
            return await self._upsert_definition(
                datasource, ddl_name, digest.kind, sql, dialect, digest,
            )
        # Plain statement → record as historical consumption
        await self._upsert_query(datasource, sql, dialect, digest)
        return True

    async def _upsert_definition(
        self,
        datasource: str,
        name: str,
        kind: str,
        sql: str,
        dialect: str,
        digest: QueryDigest,
    ) -> bool:
        conn = await self._conn()
        try:
            now = _now()
            await conn.execute(
                """INSERT INTO lineage_definitions
                   (datasource, name, kind, sql, dialect, digest_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(datasource, name) DO UPDATE SET
                     kind=excluded.kind, sql=excluded.sql, dialect=excluded.dialect,
                     digest_json=excluded.digest_json, updated_at=excluded.updated_at""",
                (datasource, name, kind, sql, dialect, _digest_json(digest), now, now),
            )
            await conn.commit()
        finally:
            await conn.close()
        return True

    async def record_query(self, sql: str, datasource: str, dialect: str = "sqlite") -> None:
        """Record an executed query (resource-used facts). Deduped by shard."""
        digest = analyze_query(sql, dialect)
        if digest is None or not digest.tables_read:
            return
        await self._upsert_query(datasource, sql, dialect, digest)

    async def _upsert_query(
        self, datasource: str, sql: str, dialect: str, digest: QueryDigest,
    ) -> None:
        shard = normalization_key(sql)
        conn = await self._conn()
        try:
            async with await conn.execute(
                "SELECT id, runs FROM lineage_query_log WHERE datasource = ? AND shard = ?",
                (datasource, shard),
            ) as cursor:
                row = await cursor.fetchone()
            now = _now()
            if row is None:
                await conn.execute(
                    """INSERT INTO lineage_query_log
                       (datasource, shard, sql, dialect, digest_json, first_seen, last_seen, runs)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (datasource, shard, sql, dialect, _digest_json(digest), now, now, 1),
                )
            else:
                await conn.execute(
                    """UPDATE lineage_query_log SET
                       sql=?, dialect=?, digest_json=?, last_seen=?, runs=runs+1
                       WHERE datasource = ? AND shard = ?""",
                    (sql, dialect, _digest_json(digest), now, datasource, shard),
                )
            await conn.commit()
        finally:
            await conn.close()

    async def _load_definitions_yaml(self, datasource: str) -> None:
        """Copy definitions.yml entries into the SQLite store (mtime gated)."""
        path = self.definitions_yaml(datasource)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return
        conn = await self._conn()
        try:
            async with await conn.execute(
                "SELECT mtime FROM lineage_sync WHERE file_path = ?", (str(path),),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None and row[0] == mtime:
                return
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            entries = data.get("definitions", []) or []
            # Rebuild from source: stale entries from this datasource are dropped
            await conn.execute(
                "DELETE FROM lineage_definitions WHERE datasource = ?", (datasource,),
            )
            await conn.execute(
                "DELETE FROM lineage_query_log WHERE datasource = ? AND shard LIKE 'def:%'",
                (datasource,),
            )
            for entry in entries:
                sql = str(entry.get("sql", "")).strip()
                if not sql:
                    continue
                digest = analyze_query(sql, entry.get("dialect", "sqlite"))
                if digest is None:
                    continue
                if digest.kind in ("create_view", "create_table_as") and digest.name:
                    await conn.execute(
                        """INSERT INTO lineage_definitions
                           (datasource, name, kind, sql, dialect, digest_json, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(datasource, name) DO UPDATE SET
                             kind=excluded.kind, sql=excluded.sql,
                             dialect=excluded.dialect, digest_json=excluded.digest_json,
                             updated_at=excluded.updated_at""",
                        (
                            datasource, digest.name, digest.kind, sql,
                            entry.get("dialect", "sqlite"),
                            _digest_json(digest), _now(), _now(),
                        ),
                    )
                else:
                    # Definition-file queries are stable consumers → shard 'def:<norm>'
                    shard = "def:" + (entry.get("name", "") or normalization_key(sql))[:64]
                    await conn.execute(
                        """INSERT INTO lineage_query_log
                           (datasource, shard, sql, dialect, digest_json, first_seen, last_seen, runs)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(datasource, shard) DO UPDATE SET
                             sql=excluded.sql, dialect=excluded.dialect,
                             digest_json=excluded.digest_json,
                             last_seen=excluded.last_seen,
                             runs=lineage_query_log.runs + 1""",
                        (
                            datasource, shard, sql, entry.get("dialect", "sqlite"),
                            _digest_json(digest), _now(), _now(), 1,
                        ),
                    )
            await conn.execute(
                "INSERT INTO lineage_sync (file_path, mtime) VALUES (?, ?) "
                "ON CONFLICT(file_path) DO UPDATE SET mtime = excluded.mtime",
                (str(path), mtime),
            )
            await conn.commit()
            logger.info("lineage: synced definitions for %s (%d entries)", datasource, len(entries))
        finally:
            await conn.close()

    async def ensure_synced(self, datasource: str | None) -> None:
        """Lazy sync of definitions.yml for the active datasource."""
        if not datasource:
            return
        await self._load_definitions_yaml(datasource)

    async def clear(self, datasource: str | None = None) -> None:
        """Drop lineage facts (optionally scoped to a datasource)."""
        conn = await self._conn()
        try:
            if datasource:
                await conn.execute("DELETE FROM lineage_definitions WHERE datasource = ?", (datasource,))
                await conn.execute("DELETE FROM lineage_query_log WHERE datasource = ?", (datasource,))
            else:
                await conn.execute("DELETE FROM lineage_definitions")
                await conn.execute("DELETE FROM lineage_query_log")
            await conn.commit()
        finally:
            await conn.close()

    # ── Retrieval (deterministic) ─────────────────────────

    async def _definitions(self, datasource: str) -> list[dict[str, Any]]:
        conn = await self._conn()
        try:
            async with await conn.execute(
                "SELECT name, kind, sql, dialect, digest_json, created_at, updated_at "
                "FROM lineage_definitions WHERE datasource = ? ORDER BY name",
                (datasource,),
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await conn.close()
        return [
            {
                "name": r[0], "kind": r[1], "sql": r[2], "dialect": r[3],
                "created_at": r[5], "updated_at": r[6],
                "tables_read": _tables_of(r[4]),
                "outputs": _outputs_of(r[4]),
            }
            for r in rows
        ]

    async def _query_log(self, datasource: str) -> list[dict[str, Any]]:
        conn = await self._conn()
        try:
            async with await conn.execute(
                "SELECT sql, dialect, digest_json, first_seen, last_seen, runs "
                "FROM lineage_query_log WHERE datasource = ? ORDER BY last_seen DESC",
                (datasource,),
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await conn.close()
        return [
            {
                "sql": r[0], "dialect": r[1], "first_seen": r[3],
                "last_seen": r[4], "runs": r[5],
                "tables_read": _tables_of(r[2]),
                "columns_read": _columns_of(r[2]),
            }
            for r in rows
        ]

    async def table_upstream(self, datasource: str, table: str) -> list[dict[str, Any]]:
        """Producers of ``table`` — named definitions whose output IS that table."""
        await self.ensure_synced(datasource)
        return [
            d for d in await self._definitions(datasource)
            if d["name"].lower() == table.lower()
        ]

    async def table_downstream(self, datasource: str, table: str) -> list[dict[str, Any]]:
        """Consumers of ``table`` — definitions + recorded queries reading it."""
        await self.ensure_synced(datasource)
        t = table.lower()
        consumers: list[dict[str, Any]] = []
        for d in await self._definitions(datasource):
            # Definitions read their base tables directly (tables_read) or
            # reference them transitively through sources
            if any(rt.lower() == t for rt in d["tables_read"]):
                consumers.append({"kind": d["kind"], "name": d["name"], "sql": d["sql"]})
            elif any(s["table"].lower() == t for out in d["outputs"] for s in out["sources"]):
                consumers.append({"kind": d["kind"], "name": d["name"], "sql": d["sql"]})
        for q in await self._query_log(datasource):
            if any(rt.lower() == t for rt in q["tables_read"]):
                consumers.append({
                    "kind": "query", "name": "", "sql": q["sql"],
                    "last_seen": q["last_seen"], "runs": q["runs"],
                })
        return consumers

    async def column_lineage(
        self, datasource: str, table: str, column: str,
    ) -> dict[str, Any]:
        """Producers and consumers of one cell (table.column).

        Producers = definitions named ``table`` whose projection emits ``column``,
        with the contributing source columns (SQLGlot column mapping). Returns the
        empty dict when nothing is recorded (caller renders "no lineage found").
        """
        await self.ensure_synced(datasource)
        t, c = table.lower(), column.lower()
        producers: list[dict[str, Any]] = []
        for d in await self._definitions(datasource):
            if d["name"].lower() != t:
                continue
            for out in d["outputs"]:
                if out["name"].lower() == c:
                    producers.append({
                        "kind": d["kind"], "name": d["name"],
                        "sql": d["sql"], "dialect": d["dialect"],
                        "sources": out["sources"],
                        "updated_at": d["updated_at"],
                    })
        consumers: list[dict[str, Any]] = []
        for q in await self._query_log(datasource):
            if any(
                rt.lower() == t and rc.lower() == c for rt, rc in q["columns_read"]
            ):
                consumers.append({"sql": q["sql"], "last_seen": q["last_seen"], "runs": q["runs"]})
        return {"producers": producers, "consumers": consumers}

    async def table_columns_index(self, datasource: str, table: str) -> list[str]:
        """Columns of ``table`` known from producer definitions (for target matching)."""
        stored = await self.table_upstream(datasource, table)
        cols: list[str] = []
        for d in stored:
            cols.extend(o["name"] for o in d["outputs"])
        return sorted({c for c in cols if c})

    async def known_tables(self, datasource: str) -> list[str]:
        """Every table name the lineage store knows (produced + consumed).

        Used to resolve lineage question targets beyond the physical
        catalog (views and ETL outputs live here, not necessarily in
        ``get_schema``).
        """
        await self.ensure_synced(datasource)
        names: set[str] = set()
        for d in await self._definitions(datasource):
            if d["name"]:
                names.add(d["name"])
            names.update(d["tables_read"])
        for q in await self._query_log(datasource):
            names.update(q["tables_read"])
        return sorted(names)


# ── Convenience parallel helpers (match KB service style) ──


async def build_lineage_service(project_root: str | Path) -> LineageService:
    """Async factory kept for symmetry; construction is synchronous today."""
    return LineageService(project_root)