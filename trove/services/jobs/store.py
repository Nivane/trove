"""Scheduled job persistence — cross-session, per-project SQLite store.

Store: ``.trove/jobs/jobs.sqlite`` (mirrors the KB/Session mirror pattern).
Tables:
  jobs   — one scheduled question per row (schedule + optional alert rule)
  runs   — execution history for job status & alert dedup

Jobs are plain read-only questions by default; HITL is bypassed for
scheduled runs (auto-approved) since the agent only ever executes
SELECTs through the read-only execution channel.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from trove.core.logging import get_logger

logger = get_logger(__name__)

JOBS_DIR_NAME = "jobs"

_CREATE_JOBS = """CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    question TEXT NOT NULL,
    datasource TEXT DEFAULT 'demo',
    workflow TEXT DEFAULT 'reflection',
    schedule_type TEXT NOT NULL,          -- 'interval' | 'cron'
    schedule TEXT NOT NULL,               -- minutes or cron expr
    enabled INTEGER NOT NULL DEFAULT 1,
    alert_expr TEXT DEFAULT '',
    alert_channel TEXT DEFAULT '',
    alert_cooldown_min INTEGER NOT NULL DEFAULT 30,
    next_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)"""

_CREATE_RUNS = """CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,                  -- 'ok' | 'error' | 'alert'
    alert_triggered INTEGER NOT NULL DEFAULT 0,
    alert_sent INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER,
    verdict TEXT DEFAULT '',
    result_json TEXT DEFAULT '{}'
)"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    """A scheduled question (one row of the jobs table)."""

    name: str
    question: str
    schedule: str
    schedule_type: str = "interval"  # interval (minutes) | cron
    datasource: str = "demo"
    workflow: str = "reflection"
    enabled: bool = True
    alert_expr: str = ""
    alert_channel: str = ""
    alert_cooldown_min: int = 30
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    next_run_at: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


@dataclass
class Run:
    """One execution attempt of a job."""

    job_id: str
    started_at: str = field(default_factory=now_iso)
    finished_at: str = ""
    status: str = "ok"
    alert_triggered: bool = False
    alert_sent: bool = False
    row_count: int | None = None
    verdict: str = ""
    result_json: dict[str, Any] = field(default_factory=dict)


def _job_to_row(job: Job) -> tuple:
    return (
        job.id,
        job.name,
        job.question,
        job.datasource,
        job.workflow,
        job.schedule_type,
        job.schedule,
        1 if job.enabled else 0,
        job.alert_expr,
        job.alert_channel,
        job.alert_cooldown_min,
        job.next_run_at,
        job.created_at,
        job.updated_at,
    )


def _row_to_job(row) -> Job:
    return Job(
        id=row[0], name=row[1], question=row[2], datasource=row[3],
        workflow=row[4], schedule_type=row[5], schedule=row[6],
        enabled=bool(row[7]), alert_expr=row[8], alert_channel=row[9],
        alert_cooldown_min=row[10], next_run_at=row[11] or "",
        created_at=row[12], updated_at=row[13],
    )


class JobStore:
    def __init__(self, project_root: str | Path, jobs_dir: str | Path | None = None):
        self.root = Path(project_root)
        self.jobs_dir = (
            Path(jobs_dir) if jobs_dir is not None
            else self.root / ".trove" / JOBS_DIR_NAME
        )
        self.db_path = self.jobs_dir / "jobs.sqlite"

    async def _conn(self) -> aiosqlite.Connection:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self.db_path))
        await conn.execute(_CREATE_JOBS)
        await conn.execute(_CREATE_RUNS)
        await conn.commit()
        return conn

    # ── jobs ─────────────────────────────────────────────

    async def save_job(self, job: Job) -> None:
        job.updated_at = now_iso()
        conn = await self._conn()
        try:
            await conn.execute(
                """INSERT INTO jobs (id, name, question, datasource, workflow,
                   schedule_type, schedule, enabled, alert_expr, alert_channel,
                   alert_cooldown_min, next_run_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, question=excluded.question,
                     datasource=excluded.datasource, workflow=excluded.workflow,
                     schedule_type=excluded.schedule_type, schedule=excluded.schedule,
                     enabled=excluded.enabled, alert_expr=excluded.alert_expr,
                     alert_channel=excluded.alert_channel,
                     alert_cooldown_min=excluded.alert_cooldown_min,
                     next_run_at=excluded.next_run_at, updated_at=excluded.updated_at""",
                _job_to_row(job),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def load_jobs(self) -> list[Job]:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC"
            )
            jobs = [_row_to_job(r) async for r in cursor]
        finally:
            await conn.close()
        return jobs

    async def get_job(self, job_id: str) -> Job | None:
        conn = await self._conn()
        try:
            async with conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,),
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await conn.close()
        return _row_to_job(row) if row else None

    async def delete_job(self, job_id: str) -> bool:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "DELETE FROM jobs WHERE id = ?", (job_id,),
            )
            await conn.commit()
            return cursor.rowcount > 0
        finally:
            await conn.close()

    # ── runs ─────────────────────────────────────────────

    async def start_run(self, run: Run) -> int:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                """INSERT INTO runs (job_id, started_at, finished_at, status,
                   alert_triggered, alert_sent, row_count, verdict, result_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    run.job_id, run.started_at, run.finished_at, run.status,
                    1 if run.alert_triggered else 0,
                    1 if run.alert_sent else 0,
                    run.row_count, run.verdict,
                    json.dumps(run.result_json, ensure_ascii=False),
                ),
            )
            await conn.commit()
            return int(cursor.lastrowid)
        finally:
            await conn.close()

    async def finish_run(
        self, run_id: int, status: str, alert_triggered: bool,
        alert_sent: bool, row_count: int | None = None, verdict: str = "",
    ) -> None:
        conn = await self._conn()
        try:
            await conn.execute(
                """UPDATE runs SET finished_at=?, status=?, alert_triggered=?,
                   alert_sent=?, row_count=?, verdict=? WHERE id=?""",
                (
                    now_iso(), status, 1 if alert_triggered else 0,
                    1 if alert_sent else 0, row_count, verdict, run_id,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def recent_run(self, job_id: str) -> dict[str, Any] | None:
        """Most recent run row for a job (alert dedup + status display)."""
        conn = await self._conn()
        try:
            async with conn.execute(
                """SELECT id, started_at, finished_at, status, alert_triggered,
                   alert_sent, row_count, verdict, result_json
                   FROM runs WHERE job_id = ? ORDER BY id DESC LIMIT 1""",
                (job_id,),
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await conn.close()
        if not row:
            return None
        return {
            "id": row[0], "started_at": row[1], "finished_at": row[2],
            "status": row[3], "alert_triggered": bool(row[4]),
            "alert_sent": bool(row[5]), "row_count": row[6],
            "verdict": row[7], "result": json.loads(row[8] or "{}"),
        }