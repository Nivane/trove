"""漂移的持久化 —— ``semantic_drift`` + ``drift_run`` 两张表。

**为什么必须有这一层**:在它之前,漂移是「报完就结束」的 —— ``check_drift.py``
exit 1 即终止,下次跑还是同一批名字。于是**无法区分「新漂移」与「已知未处理」**,
而这两者对运维是完全不同的信号。告警疲劳就是这么来的:门禁天天红,红到没人看。
有了 ``first_seen_at`` / ``seen_count``,「这条昨天就有」与「这条刚冒出来」在数据
上就是两件事。

**I5 幂等**:``UNIQUE (datasource, level, kind, subject)`` + upsert。同一个主体
重复检测只刷新 ``last_seen_at`` / ``seen_count``,不会长出第二行。

**重现已解决项 = 回归**。``resolved`` 的条目被再次检出时**重开为 open** ——
这正是「95% 上线、业务跑着跑着会降下来」的信号,把它压在 resolved 里等于把系统
存在的理由藏起来。而 ``waived`` **保持 waived**:豁免是人对「这条我认了」的决定,
再次检出是预期之内,不代表决定失效。

两者的区别是**判断的性质不同**:

- ``resolved`` 是对**世界**的断言(「已经不漂了」)—— 世界变了,断言就失效;
- ``waived`` 是对**我们立场**的断言(「这条不管」)—— 世界没变,立场就没变。

**一次 skipped 的检测不碰任何条目**。它只写一行 ``drift_run``。条目的
``last_seen_at`` 停留在上一次**成功**检测的时刻 —— 这是诚实的:我们确实没有
在那次看到它们。若把 skipped 也刷一遍 ``last_seen_at``,「未检测」就又被洗成了
「已确认仍在」,与 I3 同一条错误。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from trove.core.logging import get_logger
from trove.services.drift.models import (
    ITEM_STATUSES,
    RUN_OK,
    SOURCE_DETECTOR,
    SOURCE_EXTERNAL,
    STATUS_OPEN,
    STATUS_RESOLVED,
    STATUS_WAIVED,
    DriftItem,
    DriftReport,
    DriftRow,
    ImpactSet,
    severity_rank,
)

logger = get_logger(__name__)

DRIFT_DIR_NAME = "drift"
STORE_NAME = "drift"

#: SELECT 的列顺序 = :func:`_row_to_drift` 的位置参数顺序。
#: 两处必须一起改 —— 用 ``SELECT *`` 会在加列时静默错位。
_COLUMNS = (
    "id, datasource, level, kind, subject, detail, affected, severity, "
    "status, source, first_seen_at, last_seen_at, seen_count, "
    "resolved_at, resolved_by, resolve_reason"
)

_SEMANTIC_DRIFT_SQL = """
CREATE TABLE IF NOT EXISTS semantic_drift (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource      TEXT    NOT NULL,
    level           TEXT    NOT NULL,
    kind            TEXT    NOT NULL,
    subject         TEXT    NOT NULL,
    detail          TEXT    NOT NULL DEFAULT '{}',
    affected        TEXT    NOT NULL DEFAULT '{}',
    severity        TEXT    NOT NULL,
    status          TEXT    NOT NULL,
    source          TEXT    NOT NULL DEFAULT 'detector',
    first_seen_at   TEXT    NOT NULL,
    last_seen_at    TEXT    NOT NULL,
    seen_count      INTEGER NOT NULL DEFAULT 1,
    resolved_at     TEXT,
    resolved_by     TEXT,
    resolve_reason  TEXT,
    UNIQUE (datasource, level, kind, subject)
)
"""

_DRIFT_RUN_SQL = """
CREATE TABLE IF NOT EXISTS drift_run (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    datasource   TEXT    NOT NULL,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    status       TEXT    NOT NULL,
    skip_reason  TEXT,
    detected     INTEGER NOT NULL DEFAULT 0,
    new_count    INTEGER NOT NULL DEFAULT 0
)
"""

_DRIFT_STATUS_IDX = ("CREATE INDEX IF NOT EXISTS idx_drift_status "
                     "ON semantic_drift (datasource, status)")
_DRIFT_SEEN_IDX = ("CREATE INDEX IF NOT EXISTS idx_drift_seen "
                   "ON semantic_drift (datasource, last_seen_at)")
_DRIFT_RUN_IDX = ("CREATE INDEX IF NOT EXISTS idx_drift_run "
                  "ON drift_run (datasource, started_at)")

def _migrations():
    from trove.storage.migrations import Migration

    return [
        Migration(
            version=1,
            description="semantic_drift + drift_run 两张表与索引",
            ops=[_SEMANTIC_DRIFT_SQL, _DRIFT_RUN_SQL,
                 _DRIFT_STATUS_IDX, _DRIFT_SEEN_IDX, _DRIFT_RUN_IDX],
        ),
    ]


def _row_to_drift(row: Any) -> DriftRow:
    return DriftRow(
        id=row[0],
        datasource=row[1],
        level=row[2],
        kind=row[3],
        subject=row[4],
        detail=_loads(row[5]),
        affected=_loads(row[6]),
        severity=row[7],
        status=row[8],
        source=row[9],
        first_seen_at=row[10],
        last_seen_at=row[11],
        seen_count=row[12],
        resolved_at=row[13],
        resolved_by=row[14],
        resolve_reason=row[15],
    )


def _loads(raw: Any) -> dict:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


def _dumps(val: Any) -> str:
    return json.dumps(val or {}, ensure_ascii=False, sort_keys=True)


class DriftStore:
    """``<root>/.trove/drift/drift.sqlite``(StorageBackend-backed)。

    走 :func:`~trove.storage.migrations.apply_migrations` 而非每次打开都探测:
    见 ``storage/migrations.py`` 的模块 docstring —— 版本要反映**历史**,不是
    反映每次启动时「看起来能用」。
    """

    def __init__(self, project_root: str | Path,
                 drift_dir: str | Path | None = None):
        self.root = Path(project_root)
        self.drift_dir = (
            Path(drift_dir) if drift_dir is not None
            else self.root / ".trove" / DRIFT_DIR_NAME
        )
        self.db_path = self.drift_dir / "drift.sqlite"
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

    async def record(
        self,
        report: DriftReport,
        *,
        impact: Callable[[DriftItem], ImpactSet] | None = None,
    ) -> int:
        """把一次检测落库,返回 ``new_count``(本次首见的条目数)。

        一次事务:``drift_run`` 一行 + 条目若干。原子性在这里是必要的 ——
        一份「run 记了 ok 但条目没落地」的记录会让下次查询以为查过了。
        """
        conn = await self._conn()
        try:
            if report.status != RUN_OK:
                # 未检测:只记 run,**不碰任何条目**。理由见模块 docstring。
                await conn.execute(
                    "INSERT INTO drift_run (datasource, started_at, finished_at,"
                    " status, skip_reason, detected, new_count)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (report.datasource, report.generated_at, report.generated_at,
                     report.status, report.skip_reason, 0, 0),
                )
                await conn.commit()
                logger.info(
                    "drift run skipped: datasource=%s reason=%s",
                    report.datasource, report.skip_reason,
                )
                return 0

            existing = await self._existing_map(conn, report.datasource)
            new_count = 0
            for item in report.items:
                key = (item.level, item.kind, item.subject)
                prior = existing.get(key)
                affected = (impact(item) if impact is not None else ImpactSet())
                if prior is None:
                    new_count += 1
                await self._upsert(conn, report, item, prior, affected)

            await conn.execute(
                "INSERT INTO drift_run (datasource, started_at, finished_at,"
                " status, skip_reason, detected, new_count) VALUES (?,?,?,?,?,?,?)",
                (report.datasource, report.generated_at, report.generated_at,
                 RUN_OK, None, len(report.items), new_count),
            )
            await conn.commit()
            logger.info(
                "drift run: datasource=%s detected=%d new=%d",
                report.datasource, len(report.items), new_count,
            )
            return new_count
        finally:
            await conn.close()

    async def _existing_map(self, conn, datasource: str) -> dict[tuple, DriftRow]:
        """该数据源现存条目,按 (level, kind, subject) 索引。

        先读再写(而不是纯 upsert)是为了拿到 ``status`` 决定重开语义,并把
        上一次的处理理由并进 ``detail`` —— 纯 upsert 的 ``DO UPDATE`` 表达不了
        这种合并。

        **复用调用方的连接**,不自己开:一是读与写要在同一事务里(否则并发下
        读完到写之间状态会变),二是 ``StorageBackend`` 的操作作用域按 task
        持有 —— 同一个 task 再取一次是没必要的风险。
        """
        cursor = await conn.execute(
            f"SELECT {_COLUMNS} FROM semantic_drift WHERE datasource = ?",
            (datasource,),
        )
        rows = [_row_to_drift(r) async for r in cursor]
        return {(r.level, r.kind, r.subject): r for r in rows}

    async def _upsert(self, conn, report: DriftReport, item: DriftItem,
                      prior: DriftRow | None, affected: ImpactSet) -> None:
        detail = dict(item.detail)
        if prior is not None and prior.status == STATUS_RESOLVED:
            # 重开前把上次的处理理由并进 detail —— 否则「谁在什么时候说解决了、
            # 结果没解决」这条最有用的复盘线索会被覆盖掉。
            detail["reopened"] = {
                "previous_resolved_at": prior.resolved_at,
                "previous_resolved_by": prior.resolved_by,
                "previous_reason": prior.resolve_reason,
            }
        params = (
            report.datasource, item.level, item.kind, item.subject,
            _dumps(detail), _dumps(affected.to_dict()), item.severity,
            STATUS_OPEN, SOURCE_DETECTOR, report.generated_at, report.generated_at,
        )
        await conn.execute(
            """INSERT INTO semantic_drift
               (datasource, level, kind, subject, detail, affected, severity,
                status, source, first_seen_at, last_seen_at, seen_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
               ON CONFLICT (datasource, level, kind, subject) DO UPDATE SET
                 detail        = excluded.detail,
                 affected      = excluded.affected,
                 severity      = excluded.severity,
                 last_seen_at  = excluded.last_seen_at,
                 seen_count    = semantic_drift.seen_count + 1,
                 status        = CASE
                     WHEN semantic_drift.status = ? THEN ?
                     ELSE semantic_drift.status END,
                 resolved_at   = CASE
                     WHEN semantic_drift.status = ? THEN NULL
                     ELSE semantic_drift.resolved_at END,
                 resolved_by   = CASE
                     WHEN semantic_drift.status = ? THEN NULL
                     ELSE semantic_drift.resolved_by END,
                 resolve_reason= CASE
                     WHEN semantic_drift.status = ? THEN NULL
                     ELSE semantic_drift.resolve_reason END""",
            params + (STATUS_RESOLVED, STATUS_OPEN, STATUS_RESOLVED,
                      STATUS_RESOLVED, STATUS_RESOLVED),
        )

    async def declare_external(
        self, *, datasource: str, level: str, kind: str, subject: str,
        detail: dict, severity: str, declared_at: str, author: str = "",
    ) -> None:
        """承接一条外部声明的漂移(L4 口径变更没有检测器,只有人的声明)。

        ``source='external'`` —— 与检测器产出的条目区分开。两者的可信度来源
        不同:一条是机器比对的结论,一条是人的告知。
        """
        conn = await self._conn()
        try:
            payload = dict(detail or {})
            if author:
                payload["declared_by"] = author
            await conn.execute(
                """INSERT INTO semantic_drift
                   (datasource, level, kind, subject, detail, affected, severity,
                    status, source, first_seen_at, last_seen_at, seen_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT (datasource, level, kind, subject) DO UPDATE SET
                     detail       = excluded.detail,
                     severity     = excluded.severity,
                     last_seen_at = excluded.last_seen_at,
                     seen_count   = semantic_drift.seen_count + 1,
                     source       = excluded.source""",
                (datasource, level, kind, subject, _dumps(payload), "{}",
                 severity, STATUS_OPEN, SOURCE_EXTERNAL, declared_at, declared_at),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def set_status(self, drift_id: int, status: str, *,
                         by: str, reason: str, at: str,
                         allowed_from: tuple[str, ...]) -> bool:
        """改条目状态;当前状态不在 ``allowed_from`` 里 → 返回 False(不报错)。

        调用方据此回 409。把判定放在 SQL 的 ``WHERE`` 里而不是先读后写:
        先读后写在并发下会两次都通过检查,后写的覆盖先写的。
        """
        if status not in ITEM_STATUSES:
            raise ValueError(f"unknown item status: {status!r}")
        conn = await self._conn()
        try:
            marks = ",".join("?" for _ in allowed_from)
            cursor = await conn.execute(
                f"""UPDATE semantic_drift
                    SET status = ?, resolved_at = ?, resolved_by = ?,
                        resolve_reason = ?
                    WHERE id = ? AND status IN ({marks})""",
                (status, at, by, reason, drift_id, *allowed_from),
            )
            await conn.commit()
            return cursor.rowcount > 0
        finally:
            await conn.close()

    # ── 读取 ─────────────────────────────────────────────

    async def list_items(
        self, datasource: str, *, status: str | None = None,
        level: str | None = None, limit: int = 100,
        include_waived: bool = True,
    ) -> list[DriftRow]:
        where = ["datasource = ?"]
        params: list[Any] = [datasource]
        if status:
            where.append("status = ?")
            params.append(status)
        elif not include_waived:
            where.append("status != ?")
            params.append(STATUS_WAIVED)
        if level:
            where.append("level = ?")
            params.append(level)
        params.append(int(limit))

        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_COLUMNS} FROM semantic_drift WHERE {' AND '.join(where)}"
                f" ORDER BY severity DESC, last_seen_at DESC LIMIT ?",
                tuple(params),
            )
            rows = [_row_to_drift(r) async for r in cursor]
        finally:
            await conn.close()
        # severity 是文本列(其字典序无意义),在 Python 侧按真实严重度降序重排。
        # sorted 是稳定的,同严重度内保留 SQL 的 last_seen_at DESC 顺序。
        return sorted(rows, key=lambda r: severity_rank(r.severity), reverse=True)

    async def get_item(self, drift_id: int) -> DriftRow | None:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_COLUMNS} FROM semantic_drift WHERE id = ?",
                (drift_id,),
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()
        return _row_to_drift(row) if row is not None else None

    async def open_subjects(self, datasource: str) -> set[str]:
        """未处理(open/acknowledged)条目的主体集合 —— 门禁 coverage 判定的输入。

        ``waived`` 不在其中:豁免过的不该再阻断下一次变更。
        """
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT subject FROM semantic_drift WHERE datasource = ?"
                " AND status IN (?, ?)",
                (datasource, STATUS_OPEN, "acknowledged"),
            )
            return {r[0] async for r in cursor}
        finally:
            await conn.close()

    async def list_runs(self, datasource: str, limit: int = 20) -> list[dict]:
        """检测历史 —— **含 skipped**。I3 要求「未检测」在历史里也看得见。"""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT id, datasource, started_at, finished_at, status,"
                " skip_reason, detected, new_count FROM drift_run"
                " WHERE datasource = ? ORDER BY started_at DESC LIMIT ?",
                (datasource, int(limit)),
            )
            rows = [
                {"id": r[0], "datasource": r[1], "started_at": r[2],
                 "finished_at": r[3], "status": r[4], "skip_reason": r[5],
                 "detected": r[6], "new_count": r[7]}
                async for r in cursor
            ]
        finally:
            await conn.close()
        return rows
