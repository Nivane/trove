"""``VerdictStore`` — where each decision evaluation's audit row lives.

``<root>/.trove/decisions/verdicts.sqlite`` (or the Postgres backend when
``TROVE_STORAGE_URL`` is set), versioned through ``apply_migrations`` exactly
like every other internal store: the version records **history**, so a fresh
database, a legacy one and a retry after a failed migration all take the same
code path.

Why a store at all, rather than reading the jobs' ``result_json`` back: the
job run table is a *schedule* log — it is pruned, it mixes decision and NL
runs, and its rows are addressed by run, not by rule. "Show me the last 20
verdicts for rule X, and what changed between each pair" is a query about
verdicts; making it a scan over run rows would be slow now and wrong later.

**Verdicts are append-only.** Every write method is an INSERT; there is no
update and no delete except retention purging. A verdict records what was
true at a moment, and rows that can be edited cannot be evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.services.decision.verdicts import VerdictRecord

logger = get_logger(__name__)

DECISIONS_DIR_NAME = "decisions"
STORE_NAME = "decision_verdicts"

#: SELECT 的列顺序 = :func:`_row_to_verdict` 的位置参数顺序。
#: 两处必须一起改 —— 用 ``SELECT *`` 会在加列时静默错位。
_COLUMNS = (
    "id, datasource, rule_id, rule_digest, run_id, job_id, status, triggered,"
    " severity, priority, message, error, row_count, evidence_json,"
    " evidence_truncated, anchor_date, evaluated_at, created_at"
)

_VERDICTS_SQL = """
CREATE TABLE IF NOT EXISTS verdicts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource         TEXT    NOT NULL,
    rule_id            TEXT    NOT NULL,
    rule_digest        TEXT    NOT NULL DEFAULT '',
    run_id             INTEGER,
    job_id             TEXT    NOT NULL DEFAULT '',
    status             TEXT    NOT NULL,
    triggered          INTEGER NOT NULL DEFAULT 0,
    severity           TEXT    NOT NULL DEFAULT '',
    priority           INTEGER NOT NULL DEFAULT 0,
    message            TEXT    NOT NULL DEFAULT '',
    error              TEXT    NOT NULL DEFAULT '',
    row_count          INTEGER NOT NULL DEFAULT 0,
    evidence_json      TEXT    NOT NULL DEFAULT '{}',
    evidence_truncated INTEGER NOT NULL DEFAULT 0,
    anchor_date        TEXT    NOT NULL DEFAULT '',
    evaluated_at       TEXT    NOT NULL,
    created_at         TEXT    NOT NULL
)
"""

_VERDICT_RULE_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_verdict_rule "
    "ON verdicts (datasource, rule_id, evaluated_at DESC)"
)
_VERDICT_DS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_verdict_ds "
    "ON verdicts (datasource, evaluated_at DESC)"
)
_VERDICT_RUN_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_verdict_run ON verdicts (run_id)"
)


def _migrations():
    from trove.storage.migrations import Migration

    return [
        Migration(
            version=1,
            description="verdicts 表与三个索引(规则/数据源/run 三个查询方向)",
            ops=[_VERDICTS_SQL, _VERDICT_RULE_IDX, _VERDICT_DS_IDX,
                 _VERDICT_RUN_IDX],
        ),
    ]


def _row_to_verdict(row: Any) -> VerdictRecord:
    return VerdictRecord(
        id=row[0],
        datasource=row[1],
        rule_id=row[2],
        rule_digest=row[3] or "",
        run_id=row[4],
        job_id=row[5] or "",
        status=row[6],
        triggered=bool(row[7]),
        severity=row[8] or "",
        priority=int(row[9] or 0),
        message=row[10] or "",
        error=row[11] or "",
        row_count=int(row[12] or 0),
        evidence=_loads(row[13]),
        evidence_truncated=bool(row[14]),
        anchor_date=row[15] or "",
        evaluated_at=row[16],
        created_at=row[17],
    )


def _loads(raw: Any) -> dict:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


class VerdictStore:
    """Append-only verdict history for one project tree."""

    def __init__(self, project_root: str | Path,
                 decisions_dir: str | Path | None = None):
        self.root = Path(project_root)
        self.decisions_dir = (
            Path(decisions_dir) if decisions_dir is not None
            else self.root / ".trove" / DECISIONS_DIR_NAME
        )
        self.db_path = self.decisions_dir / "verdicts.sqlite"
        from trove.storage.backends import resolve_backend

        self._backend = resolve_backend(str(self.db_path))
        self._schema_ready = False

    async def _conn(self):
        await self._ensure_schema()
        return self._backend

    async def dispose(self) -> None:
        """释放后端共享连接(进程/测试收尾)。

        aiosqlite 的工作线程不是守护线程 —— 不关连接进程会挂住不退出。
        """
        try:
            await self._backend.dispose()
        except Exception:
            pass

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        from trove.storage.migrations import POSTGRES, SQLITE, apply_migrations

        is_pg = "Postgres" in type(self._backend).__name__
        await apply_migrations(
            self._backend, STORE_NAME, _migrations(),
            dialect=POSTGRES if is_pg else SQLITE,
        )
        self._schema_ready = True

    # ── 写入 ─────────────────────────────────────────────

    async def record(self, verdict: VerdictRecord) -> int:
        """Append one verdict; returns its row id."""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                """INSERT INTO verdicts
                   (datasource, rule_id, rule_digest, run_id, job_id, status,
                    triggered, severity, priority, message, error, row_count,
                    evidence_json, evidence_truncated, anchor_date,
                    evaluated_at, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    verdict.datasource, verdict.rule_id, verdict.rule_digest,
                    verdict.run_id, verdict.job_id, verdict.status,
                    1 if verdict.triggered else 0, verdict.severity,
                    int(verdict.priority or 0), verdict.message, verdict.error,
                    int(verdict.row_count or 0),
                    json.dumps(verdict.evidence or {}, ensure_ascii=False,
                               sort_keys=True, default=str),
                    1 if verdict.evidence_truncated else 0,
                    verdict.anchor_date, verdict.evaluated_at,
                    verdict.created_at or verdict.evaluated_at,
                ),
                need_lastrowid=True,
            )
            await conn.commit()
            return int(getattr(cursor, "lastrowid", 0) or 0)
        finally:
            await conn.close()

    async def purge_before(self, cutoff_iso: str) -> int:
        """Retention: delete verdicts evaluated strictly before ``cutoff_iso``.

        Only called when ``decision.verdict_retention_days`` is configured —
        default is empty, i.e. keep everything (same discipline as
        ``memory.retention_days``: an audit trail that expires by default is
        an audit trail that is missing exactly when it is needed).
        """
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "DELETE FROM verdicts WHERE evaluated_at < ?", (cutoff_iso,),
            )
            deleted = int(getattr(cursor, "rowcount", 0) or 0)
            await conn.commit()
            if deleted:
                logger.info("verdict retention: purged %d row(s) before %s",
                            deleted, cutoff_iso)
            return deleted
        finally:
            await conn.close()

    # ── 读取 ─────────────────────────────────────────────

    async def list_for_rule(
        self, datasource: str, rule_id: str, *, limit: int = 20,
        since: str | None = None,
    ) -> list[VerdictRecord]:
        """Newest-first verdicts for one rule (the diff's ordering)."""
        where = ["datasource = ?", "rule_id = ?"]
        params: list[Any] = [datasource, rule_id]
        if since:
            where.append("evaluated_at >= ?")
            params.append(since)
        params.append(int(limit))
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_COLUMNS} FROM verdicts WHERE {' AND '.join(where)}"
                " ORDER BY evaluated_at DESC, id DESC LIMIT ?",
                tuple(params),
            )
            return [_row_to_verdict(r) async for r in cursor]
        finally:
            await conn.close()

    async def list_recent(
        self, datasource: str, *, limit: int = 500,
    ) -> list[VerdictRecord]:
        """Newest-first verdicts for one datasource, all rules (B7 quality).

        The quality rollup scores a datasource, not one rule: a rule that was
        renamed or deleted still has a history, and reading per *declared*
        rule would silently drop those verdicts from the report. Uses the
        ``(datasource, evaluated_at DESC)`` index — this is that index's
        query.
        """
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_COLUMNS} FROM verdicts WHERE datasource = ?"
                " ORDER BY evaluated_at DESC, id DESC LIMIT ?",
                (datasource, int(limit)),
            )
            return [_row_to_verdict(r) async for r in cursor]
        finally:
            await conn.close()

    async def latest_for_rules(
        self, datasource: str, rule_ids: list[str],
    ) -> dict[str, VerdictRecord]:
        """``{rule_id: newest verdict}`` — one small indexed query per rule.

        A single windowed query would be one round trip, but the rule count
        per datasource is small and bounded by the file the admin edits; a
        loop over the (datasource, rule_id, evaluated_at DESC) index is the
        version that stays obviously correct on both backends.
        """
        out: dict[str, VerdictRecord] = {}
        for rid in rule_ids:
            rows = await self.list_for_rule(datasource, rid, limit=1)
            if rows:
                out[rid] = rows[0]
        return out

    async def get(self, verdict_id: int) -> VerdictRecord | None:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_COLUMNS} FROM verdicts WHERE id = ?", (int(verdict_id),),
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()
        return _row_to_verdict(row) if row is not None else None

    async def previous_for(
        self, datasource: str, rule_id: str, before: VerdictRecord,
    ) -> VerdictRecord | None:
        """The verdict evaluated immediately before ``before`` (same rule).

        Ordered by ``(evaluated_at, id)`` — same-day runs are common for a
        daily rule re-run by hand, and ``evaluated_at`` alone would then pick
        arbitrarily between them.
        """
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_COLUMNS} FROM verdicts WHERE datasource = ?"
                " AND rule_id = ? AND (evaluated_at < ? OR"
                " (evaluated_at = ? AND id < ?))"
                " ORDER BY evaluated_at DESC, id DESC LIMIT 1",
                (datasource, rule_id, before.evaluated_at, before.evaluated_at,
                 int(before.id or 0)),
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()
        return _row_to_verdict(row) if row is not None else None
