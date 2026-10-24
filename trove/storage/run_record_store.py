"""``RunRecordStore`` — the durable "what the model saw / what the system did" record.

``{home}/run_records.sqlite`` (or the Postgres backend when
``TROVE_STORAGE_URL`` is set), versioned through ``apply_migrations`` like
every other internal store: one physical DB per store name, the version row
records **history** so a fresh / legacy / retry-after-failure database all
take the same code path.

Why a store and not the existing trace files: ``runs/*.log`` keeps the last
50 runs, ``traces.jsonl`` the last 2000 lines, and the session store keeps a
200-char preview. The one run an incident review needs is exactly the one
those rings have already dropped. This store is the **complete** request-side
record, addressable by run id and retained on its own clock.

One row per run (the header) plus append-only events. The recorder
(``trove/tracing/run_record.py``) captures synchronously into memory and
flushes asynchronously; this module is the only part that talks SQL.

Write idempotence: events carry their own ``seq`` (assigned at capture time)
and insert with ``ON CONFLICT (run_id, seq) DO NOTHING``. A retried flush
therefore rewrites the same rows instead of inventing new sequence numbers —
``MAX(seq)+1`` would turn a retry into duplicate evidence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger

logger = get_logger(__name__)

STORE_NAME = "run_records"
DB_FILE_NAME = "run_records.sqlite"

_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS run_records (
    run_id           TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL DEFAULT '',
    user_id          TEXT NOT NULL DEFAULT '',
    question         TEXT NOT NULL DEFAULT '',
    workflow         TEXT NOT NULL DEFAULT '',
    datasource       TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'running',
    model            TEXT NOT NULL DEFAULT '',
    kb_rev           TEXT NOT NULL DEFAULT '',
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    finish_json      TEXT NOT NULL DEFAULT '{}',
    assembly_json    TEXT NOT NULL DEFAULT '{}',
    error            TEXT NOT NULL DEFAULT '',
    record_version   INTEGER NOT NULL DEFAULT 1,
    truncated        INTEGER NOT NULL DEFAULT 0,
    truncation_json  TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
)
"""

_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS run_record_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    node          TEXT NOT NULL DEFAULT '',
    payload_json  TEXT NOT NULL DEFAULT '{}',
    payload_bytes INTEGER NOT NULL DEFAULT 0,
    truncated     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    UNIQUE (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES run_records(run_id) ON DELETE CASCADE
)
"""

_RUNS_SESSION_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_run_records_session "
    "ON run_records (session_id, started_at DESC)"
)
_RUNS_USER_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_run_records_user "
    "ON run_records (user_id, started_at DESC)"
)
_RUNS_STARTED_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_run_records_started "
    "ON run_records (started_at)"
)

#: Header keys the store knows how to persist. Anything else a caller passes
#: is ignored (the recorder owns the shape; the store owns the columns).
_HEADER_DEFAULTS: dict[str, Any] = {
    "session_id": "",
    "user_id": "",
    "question": "",
    "workflow": "",
    "datasource": "",
    "status": "running",
    "model": "",
    "kb_rev": "",
    "finished_at": None,
    "finish_json": "{}",
    "assembly_json": "{}",
    "error": "",
    "record_version": 1,
    "truncated": 0,
    "truncation_json": "{}",
}

#: Columns the upsert overwrites. ``started_at`` / ``created_at`` are absent
#: on purpose: they describe when the run started, not the latest flush.
_UPSERT_UPDATE = (
    "session_id", "user_id", "question", "workflow", "datasource", "status",
    "model", "kb_rev", "finished_at", "finish_json", "assembly_json", "error",
    "record_version", "truncated", "truncation_json", "updated_at",
)


def utc_now() -> str:
    """UTC ISO-8601 seconds — one clock for writes *and* retention cutoffs.

    Retention compares ISO strings; a local-time write against a UTC cutoff
    would silently shift the window by the machine's offset.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrations():
    from trove.storage.migrations import Migration

    return [
        Migration(
            version=1,
            description="run_records 头表 + 事件表 + 三个查询方向索引",
            ops=[_RUNS_SQL, _EVENTS_SQL, _RUNS_SESSION_IDX, _RUNS_USER_IDX,
                 _RUNS_STARTED_IDX],
        ),
    ]


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return default
    return val if isinstance(val, type(default)) else default


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class RunRecordStore:
    """Durable run headers + events; one instance per home directory."""

    def __init__(self, home: str | Path):
        self.home = Path(home).expanduser()
        self.db_path = self.home / DB_FILE_NAME
        from trove.storage.backends import resolve_backend

        self._backend = resolve_backend(str(self.db_path))
        self._is_pg = "Postgres" in type(self._backend).__name__
        self._schema_ready = False

    # ── lifecycle ────────────────────────────────────────

    async def _conn(self):
        await self._ensure_schema()
        return self._backend

    async def dispose(self) -> None:
        """Release the backend connection (aiosqlite's worker is non-daemon)."""
        try:
            await self._backend.dispose()
        except Exception:
            pass

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        from trove.storage.migrations import POSTGRES, SQLITE, apply_migrations

        await apply_migrations(
            self._backend, STORE_NAME, _migrations(),
            dialect=POSTGRES if self._is_pg else SQLITE,
        )
        self._schema_ready = True

    async def has_data(self) -> bool:
        """Whether the store holds at least one row — **without creating it**.

        The retention leg calls this first: a disabled recorder must not make
        the sweep materialize an empty database just to discover it has
        nothing to purge.
        """
        if not self._is_pg and self.db_path != Path(":memory:"):
            if not Path(self.db_path).exists():
                return False
        try:
            cursor = await self._backend.execute(
                "SELECT 1 FROM run_records LIMIT 1",
            )
            row = await cursor.fetchone()
            await self._backend.close()
            return row is not None
        except Exception:
            # Table absent (never initialized) — same answer as "no data".
            return False

    # ── write ────────────────────────────────────────────

    async def write(self, header: dict[str, Any],
                    events: list[dict[str, Any]]) -> int:
        """Upsert the run header and append events; returns events written.

        ``events`` entries are ``{seq, kind, node?, payload?, truncated?}``.
        A caller that omits ``seq`` gets a contiguous block starting after
        the current maximum — the recorder always supplies one.
        """
        run_id = str(header.get("run_id") or "")
        if not run_id:
            return 0
        now = utc_now()
        started_at = str(header.get("started_at") or now)
        values = {
            "run_id": run_id,
            "started_at": started_at,
            "created_at": now,
            "updated_at": now,
        }
        for key, default in _HEADER_DEFAULTS.items():
            values[key] = header.get(key, default)
        values["record_version"] = int(values.get("record_version") or 1)
        values["truncated"] = 1 if values.get("truncated") else 0
        for key in ("finish_json", "assembly_json", "truncation_json"):
            raw = values.get(key)
            values[key] = raw if isinstance(raw, str) else _dump(raw or {})

        columns = list(values)
        placeholders = ",".join("?" for _ in columns)
        updates = ", ".join(f"{c}=excluded.{c}" for c in _UPSERT_UPDATE)
        sql = (
            f"INSERT INTO run_records ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (run_id) DO UPDATE SET {updates}"
        )

        conn = await self._conn()
        written = 0
        try:
            await conn.execute(sql, tuple(values[c] for c in columns))

            base = -1
            missing = [e for e in events if "seq" not in e]
            if missing:
                cursor = await conn.execute(
                    "SELECT COALESCE(MAX(seq), -1) FROM run_record_events"
                    " WHERE run_id = ?", (run_id,),
                )
                row = await cursor.fetchone()
                base = int(row[0]) if row and row[0] is not None else -1
            offset = 0
            for event in events:
                if "seq" in event:
                    seq = int(event["seq"])
                else:
                    seq = base + 1 + offset
                    offset += 1
                payload = event.get("payload") or {}
                payload_json = payload if isinstance(payload, str) else _dump(payload)
                payload_bytes = len(payload_json.encode("utf-8"))
                await conn.execute(
                    "INSERT INTO run_record_events"
                    " (run_id, seq, kind, node, payload_json, payload_bytes,"
                    "  truncated, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT (run_id, seq) DO NOTHING",
                    (
                        run_id, seq, str(event.get("kind") or ""),
                        str(event.get("node") or ""), payload_json,
                        payload_bytes, 1 if event.get("truncated") else 0,
                        str(event.get("created_at") or now),
                    ),
                )
                written += 1
            await conn.commit()
            return written
        finally:
            await conn.close()

    # ── read ─────────────────────────────────────────────

    async def get(self, run_id: str) -> dict[str, Any] | None:
        """One run with its events in capture order (None when unknown)."""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT run_id, session_id, user_id, question, workflow,"
                " datasource, status, model, kb_rev, started_at, finished_at,"
                " finish_json, assembly_json, error, record_version, truncated,"
                " truncation_json, created_at, updated_at"
                " FROM run_records WHERE run_id = ?", (str(run_id),),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            cursor = await conn.execute(
                "SELECT seq, kind, node, payload_json, payload_bytes,"
                " truncated, created_at FROM run_record_events"
                " WHERE run_id = ? ORDER BY seq", (str(run_id),),
            )
            event_rows = await cursor.fetchall()
        finally:
            await conn.close()

        header = {
            "run_id": row[0],
            "session_id": row[1] or "",
            "user_id": row[2] or "",
            "question": row[3] or "",
            "workflow": row[4] or "",
            "datasource": row[5] or "",
            "status": row[6] or "",
            "model": row[7] or "",
            "kb_rev": row[8] or "",
            "started_at": row[9],
            "finished_at": row[10],
            "finish": _loads(row[11], {}),
            "assembly": _loads(row[12], {}),
            "error": row[13] or "",
            "record_version": int(row[14] or 1),
            "truncated": bool(row[15]),
            "truncation": _loads(row[16], {}),
            "created_at": row[17],
            "updated_at": row[18],
        }
        events = [
            {
                "seq": int(r[0]),
                "kind": r[1] or "",
                "node": r[2] or "",
                "payload": _loads(r[3], {}),
                "payload_bytes": int(r[4] or 0),
                "truncated": bool(r[5]),
                "created_at": r[6],
            }
            for r in event_rows
        ]
        return {"header": header, "events": events}

    # ── retention ────────────────────────────────────────

    async def count_before(self, cutoff_iso: str) -> dict[str, int]:
        """Rows that :meth:`purge_before` would remove (dry-run surface)."""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM run_records WHERE started_at < ?",
                (cutoff_iso,),
            )
            row = await cursor.fetchone()
            runs = int(row[0]) if row and row[0] is not None else 0
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM run_record_events WHERE run_id IN"
                " (SELECT run_id FROM run_records WHERE started_at < ?)",
                (cutoff_iso,),
            )
            row = await cursor.fetchone()
            events = int(row[0]) if row and row[0] is not None else 0
            return {"runs": runs, "events": events}
        finally:
            await conn.close()

    async def purge_before(self, cutoff_iso: str) -> dict[str, int]:
        """Delete runs (and their events) started before ``cutoff_iso``.

        Events are deleted explicitly before the header: SQLite does not
        enforce the ``ON DELETE CASCADE`` declaration unless
        ``PRAGMA foreign_keys`` is on, and a purge that leaves orphan events
        behind is a leak the next purge can never see.
        """
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "DELETE FROM run_record_events WHERE run_id IN"
                " (SELECT run_id FROM run_records WHERE started_at < ?)",
                (cutoff_iso,),
            )
            events = int(getattr(cursor, "rowcount", 0) or 0)
            cursor = await conn.execute(
                "DELETE FROM run_records WHERE started_at < ?", (cutoff_iso,),
            )
            runs = int(getattr(cursor, "rowcount", 0) or 0)
            await conn.commit()
            if runs or events:
                logger.info(
                    "run-record retention: purged %d run(s) / %d event(s)"
                    " before %s", runs, events, cutoff_iso,
                )
            return {"runs": runs, "events": events}
        finally:
            await conn.close()
