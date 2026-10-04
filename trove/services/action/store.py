"""``ActionStore`` — proposals, approvals and deliveries for one project tree.

``<root>/.trove/actions/actions.sqlite`` (or the Postgres backend when
``TROVE_STORAGE_URL`` is set), versioned through ``apply_migrations`` exactly
like every other internal store — every step individually idempotent, and the
recorded version reflects history rather than config, so a fresh database, a
legacy one and a retry after a failed migration all take one code path.

Four tables, four jobs:

- ``proposals`` — the outbound item and its lifecycle. The rendered payload is
  stored here (frozen at creation): what was approved is what is sent.
- ``approvals`` — append-only human decisions. Who approved what, when, with
  what comment. Never updated, never deleted.
- ``deliveries`` — append-only outbound receipts (dispatch attempts and acks).
- ``outcomes`` — append-only effect measurements (B7): what the numbers did
  after a dispatch, one row per proposal, written by ``measure_due``.

The unique index on ``idempotency_key`` is the dedup guarantee, enforced by
the database rather than by a read-then-write in the service: two scheduler
ticks racing on the same firing would both pass a ``find_by_key`` check and
then both insert. It is a *partial* index (``WHERE idempotency_key <> ''``)
so rows without a key — there are none in v1, but the column defaults to
``''`` — cannot collide with each other.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.services.action.models import (
    ActionProposal,
    Approval,
    Delivery,
    Outcome,
)

logger = get_logger(__name__)

STORE_NAME = "action_proposals"
ACTIONS_DIR_NAME = "actions"

#: SELECT 列顺序 = :func:`_row_to_proposal` 的位置参数顺序。两处必须一起改 ——
#: 用 ``SELECT *`` 会在加列时静默错位。
_PROPOSAL_COLUMNS = (
    "id, datasource, rule_id, rule_digest, run_id, job_id, origin_kind,"
    " action_type, template, template_digest, target_json, payload_json,"
    " rationale, evidence_refs_json, severity, priority, risk, status,"
    " idempotency_key, created_by, created_at, decided_at, expires_at,"
    " dispatched_at, attempts, error"
)

#: JOIN 里引用 proposals 列时的 ``p.`` 前缀形态 —— 从常量**派生**,不手抄:
#: 两份列表逐字漂移是加列时最安静的那种错(SELECT 错位)。
_PROPOSAL_COLUMNS_P = ", ".join(
    f"p.{c.strip()}" for c in _PROPOSAL_COLUMNS.split(","))

_APPROVAL_COLUMNS = "id, proposal_id, user_id, action, comment, created_at"
_DELIVERY_COLUMNS = (
    "id, proposal_id, channel, status, http_status, response_excerpt, error,"
    " attempted_at"
)
_OUTCOME_COLUMNS = (
    "id, proposal_id, measured_at, window_start, window_end, metric,"
    " rule_rev, delta, pct, outside_band, z, method, confidence,"
    " observed_json, error"
)

_PROPOSALS_SQL = """
CREATE TABLE IF NOT EXISTS proposals (
    id                TEXT PRIMARY KEY,
    datasource        TEXT    NOT NULL DEFAULT '',
    rule_id           TEXT    NOT NULL DEFAULT '',
    rule_digest       TEXT    NOT NULL DEFAULT '',
    run_id            INTEGER,
    job_id            TEXT    NOT NULL DEFAULT '',
    origin_kind       TEXT    NOT NULL DEFAULT 'verdict',
    action_type       TEXT    NOT NULL DEFAULT 'notify',
    template          TEXT    NOT NULL DEFAULT '',
    template_digest   TEXT    NOT NULL DEFAULT '',
    target_json       TEXT    NOT NULL DEFAULT '{}',
    payload_json      TEXT    NOT NULL DEFAULT '{}',
    rationale         TEXT    NOT NULL DEFAULT '',
    evidence_refs_json TEXT   NOT NULL DEFAULT '{}',
    severity          TEXT    NOT NULL DEFAULT '',
    priority          INTEGER NOT NULL DEFAULT 0,
    risk              TEXT    NOT NULL DEFAULT 'low',
    status            TEXT    NOT NULL DEFAULT 'pending',
    idempotency_key   TEXT    NOT NULL DEFAULT '',
    created_by        TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL,
    decided_at        TEXT    NOT NULL DEFAULT '',
    expires_at        TEXT    NOT NULL DEFAULT '',
    dispatched_at     TEXT    NOT NULL DEFAULT '',
    attempts          INTEGER NOT NULL DEFAULT 0,
    error             TEXT    NOT NULL DEFAULT ''
)
"""

_PROPOSALS_STATUS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_action_proposals_status "
    "ON proposals (status, created_at DESC)"
)
_PROPOSALS_DS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_action_proposals_ds "
    "ON proposals (datasource, created_at DESC)"
)
_PROPOSALS_IDEM_IDX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_action_proposals_idem "
    "ON proposals (idempotency_key) WHERE idempotency_key <> ''"
)

_APPROVALS_SQL = """
CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    user_id     TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT '',
    comment     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
)
"""

_APPROVALS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_action_approvals_proposal "
    "ON approvals (proposal_id, id)"
)

_DELIVERIES_SQL = """
CREATE TABLE IF NOT EXISTS deliveries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id      TEXT NOT NULL,
    channel          TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'sent',
    http_status      INTEGER,
    response_excerpt TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT '',
    attempted_at     TEXT NOT NULL
)
"""

_DELIVERIES_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_action_deliveries_proposal "
    "ON deliveries (proposal_id, id)"
)

_OUTCOMES_SQL = """
CREATE TABLE IF NOT EXISTS outcomes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id   TEXT NOT NULL,
    measured_at   TEXT NOT NULL,
    window_start  TEXT NOT NULL DEFAULT '',
    window_end    TEXT NOT NULL DEFAULT '',
    metric        TEXT NOT NULL DEFAULT '',
    rule_rev      TEXT NOT NULL DEFAULT '',
    delta         REAL,
    pct           REAL,
    outside_band  INTEGER,
    z             REAL,
    method        TEXT NOT NULL DEFAULT 'its',
    confidence    REAL,
    observed_json TEXT NOT NULL DEFAULT '{}',
    error         TEXT NOT NULL DEFAULT ''
)
"""

#: 到期面查"这条提案测过没有"走 (proposal_id, id);质量汇总按 proposal
#: 的 datasource 过滤,依赖 proposals 既有的 ds 索引。
_OUTCOMES_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_action_outcomes_proposal "
    "ON outcomes (proposal_id, id)"
)

#: Columns ``update_proposal`` may touch. Everything else on a proposal is
#: either immutable (payload, key, template) or written once at insert.
_UPDATABLE = ("status", "decided_at", "dispatched_at", "attempts", "error")


def _migrations():
    from trove.storage.migrations import Migration

    return [
        Migration(
            version=1,
            description="行动支柱三表:proposals(含幂等键唯一索引)+ approvals + deliveries",
            ops=[_PROPOSALS_SQL, _PROPOSALS_STATUS_IDX, _PROPOSALS_DS_IDX,
                 _PROPOSALS_IDEM_IDX, _APPROVALS_SQL, _APPROVALS_IDX,
                 _DELIVERIES_SQL, _DELIVERIES_IDX],
        ),
        Migration(
            version=2,
            description="闭环验收(B7):outcomes 表(效果测量,一提案一行)+ 提案索引",
            ops=[_OUTCOMES_SQL, _OUTCOMES_IDX],
        ),
    ]


def _loads(raw: Any, default: dict) -> dict:
    if not raw:
        return dict(default)
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return dict(default)
    return val if isinstance(val, dict) else dict(default)


def _row_to_proposal(row: Any) -> ActionProposal:
    return ActionProposal(
        id=row[0],
        datasource=row[1] or "",
        rule_id=row[2] or "",
        rule_digest=row[3] or "",
        run_id=row[4],
        job_id=row[5] or "",
        origin_kind=row[6] or "verdict",
        action_type=row[7] or "notify",
        template=row[8] or "",
        template_digest=row[9] or "",
        target=_loads(row[10], {}),
        payload=_loads(row[11], {}),
        rationale=row[12] or "",
        evidence_refs=_loads(row[13], {}),
        severity=row[14] or "",
        priority=int(row[15] or 0),
        risk=row[16] or "low",
        status=row[17] or "pending",
        idempotency_key=row[18] or "",
        created_by=row[19] or "",
        created_at=row[20] or "",
        decided_at=row[21] or "",
        expires_at=row[22] or "",
        dispatched_at=row[23] or "",
        attempts=int(row[24] or 0),
        error=row[25] or "",
    )


def _row_to_approval(row: Any) -> Approval:
    return Approval(
        id=row[0], proposal_id=row[1], user_id=row[2] or "",
        action=row[3] or "", comment=row[4] or "", created_at=row[5] or "",
    )


def _row_to_delivery(row: Any) -> Delivery:
    return Delivery(
        id=row[0], proposal_id=row[1], channel=row[2] or "",
        status=row[3] or "sent", http_status=row[4],
        response_excerpt=row[5] or "", error=row[6] or "",
        attempted_at=row[7] or "",
    )


def _row_to_outcome(row: Any) -> Outcome:
    band = row[9]
    return Outcome(
        id=row[0], proposal_id=row[1], measured_at=row[2] or "",
        window_start=row[3] or "", window_end=row[4] or "",
        metric=row[5] or "", rule_rev=row[6] or "",
        delta=row[7], pct=row[8],
        # 0/1/NULL 三态:None 是「判不了」,绝不能折成 False。
        outside_band=None if band is None else bool(band),
        z=row[10], method=row[11] or "its", confidence=row[12],
        observed=_loads(row[13], {}), error=row[14] or "",
    )


def _dump(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)


class ActionStore:
    """Proposals / approvals / deliveries for one project tree."""

    def __init__(self, project_root: str | Path,
                 actions_dir: str | Path | None = None):
        self.root = Path(project_root)
        self.actions_dir = (
            Path(actions_dir) if actions_dir is not None
            else self.root / ".trove" / ACTIONS_DIR_NAME
        )
        self.db_path = self.actions_dir / "actions.sqlite"
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

    # ── proposals ────────────────────────────────────────

    async def create_proposal(self, p: ActionProposal) -> None:
        conn = await self._conn()
        try:
            await conn.execute(
                """INSERT INTO proposals
                   (id, datasource, rule_id, rule_digest, run_id, job_id,
                    origin_kind, action_type, template, template_digest,
                    target_json, payload_json, rationale, evidence_refs_json,
                    severity, priority, risk, status, idempotency_key,
                    created_by, created_at, decided_at, expires_at,
                    dispatched_at, attempts, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    p.id, p.datasource, p.rule_id, p.rule_digest, p.run_id,
                    p.job_id, p.origin_kind, p.action_type, p.template,
                    p.template_digest, _dump(p.target), _dump(p.payload),
                    p.rationale, _dump(p.evidence_refs), p.severity,
                    int(p.priority or 0), p.risk, p.status, p.idempotency_key,
                    p.created_by, p.created_at, p.decided_at, p.expires_at,
                    p.dispatched_at, int(p.attempts or 0), p.error,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def get_proposal(self, proposal_id: str) -> ActionProposal | None:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_PROPOSAL_COLUMNS} FROM proposals WHERE id = ?",
                (str(proposal_id),),
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()
        return _row_to_proposal(row) if row is not None else None

    async def find_by_idempotency_key(self, key: str) -> ActionProposal | None:
        """``None`` for an empty key (the partial index ignores those too)."""
        key = str(key or "")
        if not key:
            return None
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_PROPOSAL_COLUMNS} FROM proposals"
                " WHERE idempotency_key = ? LIMIT 1",
                (key,),
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()
        return _row_to_proposal(row) if row is not None else None

    async def list_proposals(
        self, *, status: str | None = None, datasource: str | None = None,
        limit: int = 50,
    ) -> list[ActionProposal]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(str(status))
        if datasource:
            where.append("datasource = ?")
            params.append(str(datasource))
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        params.append(int(limit))
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_PROPOSAL_COLUMNS} FROM proposals{clause}"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                tuple(params),
            )
            return [_row_to_proposal(r) async for r in cursor]
        finally:
            await conn.close()

    async def count_by_status(self) -> dict[str, int]:
        """``{status: n}`` — the console's badge counts (one GROUP BY)."""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT status, COUNT(*) FROM proposals GROUP BY status")
            return {str(row[0]): int(row[1] or 0) async for row in cursor}
        finally:
            await conn.close()

    async def update_proposal(self, proposal_id: str, **fields: Any) -> None:
        """Update the mutable lifecycle columns (see :data:`_UPDATABLE`)."""
        unknown = sorted(set(fields) - set(_UPDATABLE))
        if unknown:
            raise ValueError(
                f"update_proposal: unknown field(s) {', '.join(unknown)} — "
                "a proposal's payload, key and template are immutable")
        if not fields:
            return
        # 列名来自上面的闭集白名单,值一律走占位符参数 —— 表名/列名不可参数化,
        # 所以「拼进 SQL 的东西必须来自代码里的常量」是这里唯一的纪律。
        sets = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [str(proposal_id)]
        conn = await self._conn()
        try:
            await conn.execute(
                f"UPDATE proposals SET {sets} WHERE id = ?", tuple(params))
            await conn.commit()
        finally:
            await conn.close()

    async def expire_due(self, now_iso: str) -> int:
        """Pending proposals past ``expires_at`` → ``expired``. Returns the count.

        Strictly ``<``: a proposal expiring exactly now is still approvable for
        the instant it is being read.
        """
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "UPDATE proposals SET status = 'expired', decided_at = ?"
                " WHERE status = 'pending' AND expires_at <> ''"
                " AND expires_at < ?",
                (str(now_iso), str(now_iso)),
            )
            count = int(getattr(cursor, "rowcount", 0) or 0)
            await conn.commit()
            if count:
                logger.info("action proposals: expired %d pending row(s)", count)
            return count
        finally:
            await conn.close()

    # ── approvals ────────────────────────────────────────

    async def add_approval(self, a: Approval) -> int:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                """INSERT INTO approvals
                   (proposal_id, user_id, action, comment, created_at)
                   VALUES (?,?,?,?,?)""",
                (a.proposal_id, a.user_id, a.action, a.comment, a.created_at),
                need_lastrowid=True,
            )
            await conn.commit()
            return int(getattr(cursor, "lastrowid", 0) or 0)
        finally:
            await conn.close()

    async def list_approvals(self, proposal_id: str) -> list[Approval]:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_APPROVAL_COLUMNS} FROM approvals"
                " WHERE proposal_id = ? ORDER BY id",
                (str(proposal_id),),
            )
            return [_row_to_approval(r) async for r in cursor]
        finally:
            await conn.close()

    # ── deliveries ───────────────────────────────────────

    async def add_delivery(self, d: Delivery) -> int:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                """INSERT INTO deliveries
                   (proposal_id, channel, status, http_status,
                    response_excerpt, error, attempted_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (d.proposal_id, d.channel, d.status, d.http_status,
                 d.response_excerpt, d.error, d.attempted_at),
                need_lastrowid=True,
            )
            await conn.commit()
            return int(getattr(cursor, "lastrowid", 0) or 0)
        finally:
            await conn.close()

    async def list_deliveries(self, proposal_id: str) -> list[Delivery]:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_DELIVERY_COLUMNS} FROM deliveries"
                " WHERE proposal_id = ? ORDER BY id",
                (str(proposal_id),),
            )
            return [_row_to_delivery(r) async for r in cursor]
        finally:
            await conn.close()

    async def list_delivery_times(
        self, channel: str, since_iso: str,
    ) -> list[str]:
        """某通道 ``since`` 之后的**真实尝试**时刻(升序)。

        速率护栏的输入:计数不该靠一个会被重启清零的计数器,而该来自
        append-only 的回执 trail —— 它是既有的、有索引的事实源,零新列。
        只算 ``sent`` / ``failed``:``dry_run`` 什么都没发出去,``ack`` 是
        回执不是外送。
        """
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT attempted_at FROM deliveries"
                " WHERE channel = ? AND attempted_at >= ?"
                " AND status IN ('sent', 'failed')"
                " ORDER BY attempted_at",
                (str(channel), str(since_iso)),
            )
            return [str(r[0]) async for r in cursor]
        finally:
            await conn.close()

    # ── outcomes(闭环验收,B7) ──────────────────────────

    async def record_outcome(self, o: Outcome) -> int:
        """追加一行测量记录(只增不改;同一提案重复写入 = 调用方的错)。"""
        band = None if o.outside_band is None else int(bool(o.outside_band))
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                """INSERT INTO outcomes
                   (proposal_id, measured_at, window_start, window_end,
                    metric, rule_rev, delta, pct, outside_band, z, method,
                    confidence, observed_json, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (o.proposal_id, o.measured_at, o.window_start, o.window_end,
                 o.metric, o.rule_rev, o.delta, o.pct, band, o.z, o.method,
                 o.confidence, _dump(o.observed), o.error),
                need_lastrowid=True,
            )
            await conn.commit()
            return int(getattr(cursor, "lastrowid", 0) or 0)
        finally:
            await conn.close()

    async def list_outcomes(self, proposal_id: str) -> list[Outcome]:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_OUTCOME_COLUMNS} FROM outcomes"
                " WHERE proposal_id = ? ORDER BY id",
                (str(proposal_id),),
            )
            return [_row_to_outcome(r) async for r in cursor]
        finally:
            await conn.close()

    async def list_unmeasured_proposals(
        self, statuses: tuple[str, ...], *, limit: int = 50,
    ) -> list[ActionProposal]:
        """已外送、尚无测量记录的提案(LEFT JOIN,一条查询)。

        闭环验收的到期面:``status IN (...)`` ∧ 无 outcome 行 ——
        **一次测量一个结局**,测过(哪怕是 error 行)就不再是候选。
        ``dispatched_at`` 升序:等得最久的先测。
        """
        marks = ",".join("?" for _ in statuses)
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_PROPOSAL_COLUMNS_P} FROM proposals p"
                " LEFT JOIN outcomes o ON o.proposal_id = p.id"
                f" WHERE p.status IN ({marks}) AND o.id IS NULL"
                " ORDER BY p.dispatched_at, p.id LIMIT ?",
                tuple(str(s) for s in statuses) + (int(limit),),
            )
            return [_row_to_proposal(r) async for r in cursor]
        finally:
            await conn.close()

    async def list_effect_entries(
        self, datasource: str, *, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """某数据源的效果条目(JOIN proposals)—— ``decision/score.py`` 的食粮。

        ``rule_id`` 从提案行取(提案是不变的),``rule_rev`` 在结果行上
        (测量自己记的)。投影在这里做:SQL 是 store 的事,回评是纯函数
        的事。取**最近** ``limit`` 条(id 降序)—— 质量看的是当前这一段。
        """
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT p.rule_id, o.rule_rev, o.outside_band, o.error,"
                " o.measured_at, o.window_end, o.proposal_id, o.pct"
                " FROM outcomes o JOIN proposals p ON p.id = o.proposal_id"
                " WHERE p.datasource = ? ORDER BY o.id DESC LIMIT ?",
                (str(datasource), int(limit)),
            )
            out: list[dict[str, Any]] = []
            async for row in cursor:
                band = row[2]
                out.append({
                    "rule_id": str(row[0] or ""),
                    "rule_rev": str(row[1] or ""),
                    "outside_band": None if band is None else bool(band),
                    "error": str(row[3] or ""),
                    "measured_at": str(row[4] or ""),
                    "window_end": str(row[5] or ""),
                    "proposal_id": str(row[6] or ""),
                    "pct": row[7],
                })
            return out
        finally:
            await conn.close()
