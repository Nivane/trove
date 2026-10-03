"""Scheduled job persistence — cross-session, per-project SQLite store.

Store: ``.trove/jobs/jobs.sqlite`` (mirrors the KB/Session mirror pattern).
Tables:
  jobs          — one scheduled question per row (schedule + optional alert rule)
  runs          — execution history for job status & alert dedup
  subscriptions — one subscriber × job delivery subscription per row
  deliveries    — per-(subscription, run) delivery attempt log
                  (UNIQUE: a run is delivered to a subscriber at most once —
                  a retried tick/manual run gets a *new* run row, so re-sends
                  are visible as separate runs, not duplicate delivery rows)

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
    updated_at TEXT NOT NULL,
    -- Non-empty = this job runs the decision engine, not the NL pipeline.
    decision_rule TEXT DEFAULT '',
    -- 主题域收敛(空 = 不限定):随问题透传给 NL 管线。
    -- 新列一律追加在**末尾**:_row_to_job 按位置读,SQLite 的
    -- ALTER TABLE ADD COLUMN 也只能追加 —— 插进中间会让迁移库与
    -- 新建库的列序不同,同一个索引读出两个字段。
    topic TEXT DEFAULT ''
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


_CREATE_SUBSCRIPTIONS = """CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    subscriber TEXT NOT NULL,
    channel TEXT DEFAULT '',                -- '' = 继承 job.alert_channel（再退到 console）
    mode TEXT NOT NULL DEFAULT 'always',    -- 'always' | 'alert_only'
    enabled INTEGER NOT NULL DEFAULT 1,
    created_by TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)"""

_CREATE_DELIVERIES = """CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    subscriber TEXT NOT NULL,
    channel TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'sent',    -- 'sent' | 'failed'
    error TEXT DEFAULT '',
    excerpt TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(subscription_id, run_id)
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
    #: Non-empty = run the decision engine on this rule instead of the NL
    #: pipeline. Defaults to "" so a job row written before this field
    #: existed still reads back as an ordinary question job.
    decision_rule: str = ""
    #: 主题域(空 = 不限定)。查询范围在**写任务时**校验(见
    #: ``semantic_layer.manage.topic_reference_error``),运行期只透传 ——
    #: 一个悬空引用会让任务每天以同样的方式静默失败,不能等跑起来才发现。
    topic: str = ""


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


@dataclass
class Subscription:
    """One subscriber's report-delivery subscription to a scheduled job."""

    job_id: str
    subscriber: str
    channel: str = ""       # '' = 继承 job.alert_channel，再退到 console
    mode: str = "always"    # 'always' = 每期都投递 | 'alert_only' = 仅触发时
    enabled: bool = True
    created_by: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


@dataclass
class Delivery:
    """One delivery attempt of a run's report to one subscriber."""

    subscription_id: str
    job_id: str
    run_id: int
    subscriber: str
    channel: str = ""
    status: str = "sent"    # 'sent' | 'failed'
    error: str = ""
    excerpt: str = ""
    id: int | None = None
    created_at: str = field(default_factory=now_iso)


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
        job.decision_rule,
        job.topic,
    )


def _row_to_subscription(row) -> Subscription:
    return Subscription(
        id=row[0], job_id=row[1], subscriber=row[2], channel=row[3] or "",
        mode=row[4] or "always", enabled=bool(row[5]), created_by=row[6] or "",
        created_at=row[7], updated_at=row[8],
    )


def _row_to_job(row) -> Job:
    return Job(
        id=row[0], name=row[1], question=row[2], datasource=row[3],
        workflow=row[4], schedule_type=row[5], schedule=row[6],
        enabled=bool(row[7]), alert_expr=row[8], alert_channel=row[9],
        alert_cooldown_min=row[10], next_run_at=row[11] or "",
        created_at=row[12], updated_at=row[13],
        # No len() guard: _ensure_schema guarantees the column exists, and a
        # silent "" here would turn a decision job into an NL question.
        decision_rule=row[14] or "",
        topic=row[15] or "",
    )


class JobStore:
    def __init__(self, project_root: str | Path, jobs_dir: str | Path | None = None):
        self.root = Path(project_root)
        self.jobs_dir = (
            Path(jobs_dir) if jobs_dir is not None
            else self.root / ".trove" / JOBS_DIR_NAME
        )
        self.db_path = self.jobs_dir / "jobs.sqlite"
        from trove.storage.backends import resolve_backend

        self._backend = resolve_backend(str(self.db_path))
        self._schema_ready = False

    async def _conn(self):
        await self._ensure_schema()
        return self._backend

    async def dispose(self) -> None:
        """Release the backend's shared connection (process/test teardown).

        aiosqlite's worker thread is non-daemon — without closing the
        connection the process hangs on exit.
        """
        try:
            await self._backend.dispose()
        except Exception:
            pass

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        from trove.storage.backends.base import script_statements

        await self._backend.executescript(script_statements([
            _CREATE_JOBS, _CREATE_RUNS, _CREATE_SUBSCRIPTIONS, _CREATE_DELIVERIES,
        ]))
        # `jobs` predates the migration framework (bare CREATE TABLE IF NOT
        # EXISTS), so there is no version row to hang a step off. Columns are
        # added by probe instead: idempotent on every open, and a no-op once
        # present. Never let a failure pass silently — an un-added column
        # turns a decision job back into an NL question, and drops the topic
        # scope off a topic'd job (answering on more data than was asked for).
        from trove.storage.migrations import POSTGRES, SQLITE, AddColumn, ensure_column

        is_pg = "Postgres" in type(self._backend).__name__
        dialect = POSTGRES if is_pg else SQLITE
        added = False
        for column in ("decision_rule", "topic"):
            added = await ensure_column(
                self._backend,
                AddColumn("jobs", column, "TEXT DEFAULT ''", "TEXT DEFAULT ''"),
                dialect=dialect,
            ) or added
        # ensure_column is a plain write, not a migration step — the caller commits.
        if added:
            await self._backend.commit()
        self._schema_ready = True

    # ── jobs ─────────────────────────────────────────────

    async def save_job(self, job: Job) -> None:
        job.updated_at = now_iso()
        conn = await self._conn()
        try:
            await conn.execute(
                """INSERT INTO jobs (id, name, question, datasource, workflow,
                   schedule_type, schedule, enabled, alert_expr, alert_channel,
                   alert_cooldown_min, next_run_at, created_at, updated_at,
                   decision_rule, topic)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, question=excluded.question,
                     datasource=excluded.datasource, workflow=excluded.workflow,
                     schedule_type=excluded.schedule_type, schedule=excluded.schedule,
                     enabled=excluded.enabled, alert_expr=excluded.alert_expr,
                     alert_channel=excluded.alert_channel,
                     alert_cooldown_min=excluded.alert_cooldown_min,
                     next_run_at=excluded.next_run_at, updated_at=excluded.updated_at,
                     decision_rule=excluded.decision_rule, topic=excluded.topic""",
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
            async with await conn.execute(
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
                need_lastrowid=True,
            )
            await conn.commit()
            return int(cursor.lastrowid)
        finally:
            await conn.close()

    async def finish_run(
        self, run_id: int, status: str, alert_triggered: bool,
        alert_sent: bool, row_count: int | None = None, verdict: str = "",
        result_json: dict[str, Any] | None = None,
    ) -> None:
        """Close a run row. ``result_json`` is written only when given — the
        NL path stores its evidence through ``start_run`` and passes nothing.

        A decision run has no LangGraph trace, so this column *is* its audit
        record; ``default=str`` keeps one unserializable value (a stray
        Decimal from Postgres, say) from aborting the write — an exception
        here would strand the job's schedule and re-fire the alert forever.
        """
        conn = await self._conn()
        try:
            sql = """UPDATE runs SET finished_at=?, status=?, alert_triggered=?,
                   alert_sent=?, row_count=?, verdict=?"""
            args: list[Any] = [
                now_iso(), status, 1 if alert_triggered else 0,
                1 if alert_sent else 0, row_count, verdict,
            ]
            if result_json is not None:
                sql += ", result_json=?"
                args.append(json.dumps(result_json, ensure_ascii=False, default=str))
            await conn.execute(sql + " WHERE id=?", (*args, run_id))
            await conn.commit()
        finally:
            await conn.close()

    async def recent_run(self, job_id: str) -> dict[str, Any] | None:
        """Most recent run row for a job (alert dedup + status display)."""
        conn = await self._conn()
        try:
            async with await conn.execute(
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

    async def last_alert_run(self, job_id: str) -> dict[str, Any] | None:
        """Newest *finished* run that fired an alert — the cooldown anchor.

        Deliberately not ``recent_run``: the runner records a run row before
        it evaluates, so at cooldown-check time the newest row of any kind is
        always the run currently in flight (``alert_triggered`` 0,
        ``finished_at`` NULL). Asking for that one makes the cooldown a no-op
        and every tick re-sends the alert.
        """
        conn = await self._conn()
        try:
            async with await conn.execute(
                """SELECT id, started_at, finished_at, status, alert_triggered,
                   alert_sent, row_count, verdict, result_json
                   FROM runs
                   WHERE job_id = ? AND alert_triggered = 1
                     AND finished_at IS NOT NULL
                   ORDER BY id DESC LIMIT 1""",
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

    async def list_runs(self, job_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Run history for a job (newest first), for the status UI."""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                """SELECT id, started_at, finished_at, status, alert_triggered,
                   alert_sent, row_count, verdict, result_json
                   FROM runs WHERE job_id = ? ORDER BY id DESC LIMIT ?""",
                (job_id, max(1, min(int(limit), 200))),
            )
            rows = [r async for r in cursor]
        finally:
            await conn.close()
        return [
            {
                "id": r[0], "started_at": r[1], "finished_at": r[2],
                "status": r[3], "alert_triggered": bool(r[4]),
                "alert_sent": bool(r[5]), "row_count": r[6],
                "verdict": r[7], "result": json.loads(r[8] or "{}"),
            }
            for r in rows
        ]

    # ── subscriptions ────────────────────────────────────

    async def save_subscription(self, sub: Subscription) -> None:
        sub.updated_at = now_iso()
        conn = await self._conn()
        try:
            await conn.execute(
                """INSERT INTO subscriptions (id, job_id, subscriber, channel,
                   mode, enabled, created_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     channel=excluded.channel, mode=excluded.mode,
                     enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (
                    sub.id, sub.job_id, sub.subscriber, sub.channel, sub.mode,
                    1 if sub.enabled else 0, sub.created_by,
                    sub.created_at, sub.updated_at,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def load_subscriptions(self) -> list[Subscription]:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT id, job_id, subscriber, channel, mode, enabled, "
                "created_by, created_at, updated_at FROM subscriptions "
                "ORDER BY created_at DESC, id DESC"
            )
            subs = [_row_to_subscription(r) async for r in cursor]
        finally:
            await conn.close()
        return subs

    async def get_subscription(self, sub_id: str) -> Subscription | None:
        conn = await self._conn()
        try:
            async with await conn.execute(
                "SELECT id, job_id, subscriber, channel, mode, enabled, "
                "created_by, created_at, updated_at FROM subscriptions "
                "WHERE id = ?", (sub_id,),
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await conn.close()
        return _row_to_subscription(row) if row else None

    async def delete_subscription(self, sub_id: str) -> bool:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "DELETE FROM subscriptions WHERE id = ?", (sub_id,),
            )
            await conn.commit()
            return cursor.rowcount > 0
        finally:
            await conn.close()

    # ── deliveries ───────────────────────────────────────

    async def add_delivery(self, delivery: Delivery) -> None:
        """Record one delivery attempt.

        ``(subscription_id, run_id)`` is unique — insert-or-nothing, so a
        repeated call for the same run can never produce two rows (the first
        attempt's outcome is the one kept; a genuine re-send is a *new* run).
        """
        conn = await self._conn()
        try:
            await conn.execute(
                """INSERT INTO deliveries (subscription_id, job_id, run_id,
                   subscriber, channel, status, error, excerpt, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(subscription_id, run_id) DO NOTHING""",
                (
                    delivery.subscription_id, delivery.job_id, delivery.run_id,
                    delivery.subscriber, delivery.channel, delivery.status,
                    delivery.error, delivery.excerpt, delivery.created_at,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def list_deliveries(
        self, *, subscription_id: str | None = None,
        job_id: str | None = None, subscriber: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Delivery log (newest first), filtered by sub / job / subscriber."""
        where: list[str] = []
        args: list[Any] = []
        if subscription_id:
            where.append("subscription_id = ?")
            args.append(subscription_id)
        if job_id:
            where.append("job_id = ?")
            args.append(job_id)
        if subscriber:
            where.append("subscriber = ?")
            args.append(subscriber)
        sql = (
            "SELECT id, subscription_id, job_id, run_id, subscriber, channel, "
            "status, error, excerpt, created_at FROM deliveries"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        conn = await self._conn()
        try:
            cursor = await conn.execute(sql, tuple(args))
            rows = [r async for r in cursor]
        finally:
            await conn.close()
        return [
            {
                "id": r[0], "subscription_id": r[1], "job_id": r[2],
                "run_id": r[3], "subscriber": r[4], "channel": r[5],
                "status": r[6], "error": r[7], "excerpt": r[8],
                "created_at": r[9],
            }
            for r in rows
        ]