"""体检运行记录 —— ``coverage_run`` + ``coverage_result`` 两张表。

**为什么落库而不是只留在内存**:体检的报告是「这批问题现在能编译多少」
的一次测量。没有持久化,两个数字就永远只有「刚才那一眼」—— 趋势、对比上
一轮、重看缺口清单都无从谈起。题面与逐题判定一起落库,是把一次测量钉成
可复查的记录:报告里的每个计数都能从 result 行重算,不需要相信任何缓存。

三条纪律(与仓库内其他 store 同源):

- 走 :func:`~trove.storage.migrations.apply_migrations` 的版本化迁移,不每次
  打开都探测;迁移操作逐条幂等(收养路径 = 从 0 重放)。
- **活性是唯一索引,不是先查后写**:同一数据源同时只允许一条
  ``queued|running`` 的 run —— 进程内互斥挡不住跨进程(``trove serve`` 与
  管理端多实例)。并发创建的输家拿到 :class:`CoverageRunActive`,响亮报错。
- **中断不伪造完成**:心跳过期的 run 标 ``interrupted``,未处理的题补
  ``skipped`` 行。报告页永远能对齐全题,而「没跑完」与「跑完且零缺口」
  在数据上是两件事。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger

logger = get_logger(__name__)

COVERAGE_DIR_NAME = "coverage"
STORE_NAME = "coverage"

#: run 状态闭集。``queued|running`` = 活性状态(唯一索引覆盖);
#: 其余四个都是终态 —— 报告可读、可 diff、可清退。
RUN_STATUSES: tuple[str, ...] = (
    "queued", "running", "done", "error", "cancelled", "interrupted",
)
ACTIVE_STATUSES: tuple[str, ...] = ("queued", "running")
TERMINAL_STATUSES: tuple[str, ...] = ("done", "error", "cancelled", "interrupted")

#: 心跳超过这个秒数没更新 → 视为进程已死(懒恢复时标 interrupted)。
STALE_AFTER_S = 300
#: 每数据源保留的终态 run 条数(更老的连同 result 行一起清退)。
MAX_RUNS_PER_DATASOURCE = 50

#: SELECT 的列顺序 = :func:`_row_to_run` 的位置参数顺序(一起改)。
_RUN_COLUMNS = (
    "run_id, datasource, status, created_by, created_at, started_at,"
    " finished_at, heartbeat_at, cancel_requested, question_count,"
    " processed_count, model, semantic_digest, taxonomy_version,"
    " questions_json, summary_json, error, task_id"
)
_RESULT_COLUMNS = (
    "run_id, seq, question, outcome, outcome_reason, intent, plan_typed,"
    " miss_class, miss_reason, miss_component, template_name, llm_calls,"
    " elapsed_ms, gaps_json, detail_json"
)

_CREATE_RUN = """
CREATE TABLE IF NOT EXISTS coverage_run (
    run_id            TEXT PRIMARY KEY,
    datasource        TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_by        TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT,
    heartbeat_at      TEXT,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    question_count    INTEGER NOT NULL DEFAULT 0,
    processed_count   INTEGER NOT NULL DEFAULT 0,
    model             TEXT NOT NULL DEFAULT '',
    semantic_digest   TEXT NOT NULL DEFAULT '',
    taxonomy_version  INTEGER NOT NULL,
    questions_json    TEXT NOT NULL DEFAULT '[]',
    summary_json      TEXT NOT NULL DEFAULT '{}',
    error             TEXT NOT NULL DEFAULT '',
    task_id           TEXT NOT NULL DEFAULT ''
)
"""

_CREATE_RESULT = """
CREATE TABLE IF NOT EXISTS coverage_result (
    run_id          TEXT    NOT NULL,
    seq             INTEGER NOT NULL,
    question        TEXT    NOT NULL,
    outcome         TEXT    NOT NULL,
    outcome_reason  TEXT    NOT NULL DEFAULT '',
    intent          TEXT    NOT NULL DEFAULT '',
    plan_typed      INTEGER NOT NULL DEFAULT 0,
    miss_class      TEXT    NOT NULL DEFAULT '',
    miss_reason     TEXT    NOT NULL DEFAULT '',
    miss_component  TEXT    NOT NULL DEFAULT '',
    template_name   TEXT    NOT NULL DEFAULT '',
    llm_calls       INTEGER NOT NULL DEFAULT 0,
    elapsed_ms      INTEGER NOT NULL DEFAULT 0,
    gaps_json       TEXT    NOT NULL DEFAULT '[]',
    detail_json     TEXT    NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, seq)
)
"""

# 活性的**跨进程闸**:同一数据源至多一条 queued/running。部分唯一索引
# (partial unique index)SQLite 3.8+ 与 PostgreSQL 都支持;谓词里的状态
# 列表必须与 ACTIVE_STATUSES 同步(test_store 有行为断言)。
_ACTIVE_IDX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_coverage_active "
    "ON coverage_run (datasource) "
    "WHERE status IN ('queued','running')"
)
_RUN_IDX = ("CREATE INDEX IF NOT EXISTS idx_coverage_run "
            "ON coverage_run (datasource, created_at)")
_RESULT_OUTCOME_IDX = ("CREATE INDEX IF NOT EXISTS idx_coverage_result_outcome "
                       "ON coverage_result (run_id, outcome)")
_RESULT_REASON_IDX = ("CREATE INDEX IF NOT EXISTS idx_coverage_result_reason "
                      "ON coverage_result (run_id, miss_reason)")


class CoverageRunActive(RuntimeError):
    """该数据源已有活跃 run —— 并发创建的输家。"""

    def __init__(self, run_id: str):
        super().__init__(f"a coverage run is already active: {run_id}")
        self.run_id = run_id


def _migrations():
    from trove.storage.migrations import Migration

    return [
        Migration(
            version=1,
            description="coverage_run + coverage_result 两张表与索引",
            ops=[_CREATE_RUN, _CREATE_RESULT, _ACTIVE_IDX, _RUN_IDX,
                 _RESULT_OUTCOME_IDX, _RESULT_REASON_IDX],
        ),
    ]


def now_iso() -> str:
    """落库用的时间戳(UTC ISO-8601;同格式可作字典序比较)。"""
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _load(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return default
    if isinstance(default, list) and not isinstance(val, list):
        return default
    if isinstance(default, dict) and not isinstance(val, dict):
        return default
    return val


def _dumps(val: Any) -> str:
    return json.dumps(val if val is not None else {}, ensure_ascii=False,
                      sort_keys=True, default=str)


def _row_to_run(row: Any) -> dict[str, Any]:
    return {
        "run_id": row[0],
        "datasource": row[1],
        "status": row[2],
        "created_by": row[3],
        "created_at": row[4],
        "started_at": row[5],
        "finished_at": row[6],
        "heartbeat_at": row[7],
        "cancel_requested": bool(row[8]),
        "question_count": int(row[9] or 0),
        "processed_count": int(row[10] or 0),
        "model": row[11],
        "semantic_digest": row[12],
        "taxonomy_version": int(row[13] or 0),
        "questions": _load(row[14], []),
        "summary": _load(row[15], {}),
        "error": row[16],
        "task_id": row[17],
    }


def _row_to_result(row: Any) -> dict[str, Any]:
    return {
        "run_id": row[0],
        "seq": int(row[1]),
        "question": row[2],
        "outcome": row[3],
        "outcome_reason": row[4],
        "intent": row[5],
        "plan_typed": bool(row[6]),
        "miss_class": row[7],
        "miss_reason": row[8],
        "miss_component": row[9],
        "template_name": row[10],
        "llm_calls": int(row[11] or 0),
        "elapsed_ms": int(row[12] or 0),
        "gaps": _load(row[13], []),
        "detail": _load(row[14], {}),
    }


def _result_params(run_id: str, result: dict[str, Any]) -> tuple:
    return (
        run_id,
        int(result.get("seq") or 0),
        str(result.get("question") or ""),
        str(result.get("outcome") or "error"),
        str(result.get("outcome_reason") or ""),
        str(result.get("intent") or ""),
        1 if result.get("plan_typed") else 0,
        str(result.get("miss_class") or ""),
        str(result.get("miss_reason") or ""),
        str(result.get("miss_component") or ""),
        str(result.get("template_name") or ""),
        int(result.get("llm_calls") or 0),
        int(result.get("elapsed_ms") or 0),
        _dumps(result.get("gaps") or []),
        _dumps(result.get("detail") or {}),
    )


_INSERT_RESULT = (
    "INSERT INTO coverage_result (" + _RESULT_COLUMNS + ") "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT (run_id, seq) DO UPDATE SET "
    "question = excluded.question, outcome = excluded.outcome, "
    "outcome_reason = excluded.outcome_reason, intent = excluded.intent, "
    "plan_typed = excluded.plan_typed, miss_class = excluded.miss_class, "
    "miss_reason = excluded.miss_reason, "
    "miss_component = excluded.miss_component, "
    "template_name = excluded.template_name, llm_calls = excluded.llm_calls, "
    "elapsed_ms = excluded.elapsed_ms, gaps_json = excluded.gaps_json, "
    "detail_json = excluded.detail_json"
)


class CoverageStore:
    """``<root>/.trove/coverage/coverage.sqlite``(StorageBackend-backed)。

    与 ``DriftStore`` 同一范式:``resolve_backend``(``TROVE_STORAGE_URL``
    存在时走 Postgres)+ 版本化迁移 + 显式 ``dispose()``。每个操作是一个
    ``execute … commit … close`` 括号。
    """

    def __init__(self, project_root: str | Path,
                 coverage_dir: str | Path | None = None):
        self.root = Path(project_root)
        self.coverage_dir = (
            Path(coverage_dir) if coverage_dir is not None
            else self.root / ".trove" / COVERAGE_DIR_NAME
        )
        self.db_path = self.coverage_dir / "coverage.sqlite"
        from trove.storage.backends import resolve_backend

        self._backend = resolve_backend(str(self.db_path))
        self._schema_ready = False

    async def _conn(self):
        await self._ensure_schema()
        return self._backend

    async def dispose(self) -> None:
        """释放后端共享连接。

        aiosqlite 的工作线程不是守护线程 —— 不关连接,测试进程会「全过但
        不退出」。
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

    async def create_run(self, run: dict[str, Any]) -> None:
        """建一条 run(``status`` 应为 queued)。

        Raises:
            CoverageRunActive: 该数据源已有活跃 run(进程内互斥之外的第二道闸)。
        """
        conn = await self._conn()
        params = (
            str(run["run_id"]),
            str(run["datasource"]),
            str(run.get("status") or "queued"),
            str(run.get("created_by") or ""),
            str(run.get("created_at") or now_iso()),
            run.get("started_at"),
            run.get("finished_at"),
            run.get("heartbeat_at"),
            1 if run.get("cancel_requested") else 0,
            int(run.get("question_count") or 0),
            int(run.get("processed_count") or 0),
            str(run.get("model") or ""),
            str(run.get("semantic_digest") or ""),
            int(run.get("taxonomy_version") or 0),
            _dumps(list(run.get("questions") or [])),
            _dumps(dict(run.get("summary") or {})),
            str(run.get("error") or ""),
            str(run.get("task_id") or ""),
        )
        try:
            try:
                await conn.execute(
                    "INSERT INTO coverage_run (" + _RUN_COLUMNS + ") "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    params,
                )
                await conn.commit()
            finally:
                await conn.close()
        except Exception:
            # 唯一索引冲突(并发)与其它写入失败要靠**读回活跃行**区分:
            # 有活跃行 = 业务冲突(409);没有 = 真故障,原样上抛。
            active = await self.active_run(str(run["datasource"]))
            if active is not None:
                raise CoverageRunActive(active["run_id"]) from None
            raise

    async def mark_started(self, run_id: str, *, at: str | None = None) -> None:
        at = at or now_iso()
        conn = await self._conn()
        try:
            await conn.execute(
                "UPDATE coverage_run SET status = 'running', started_at = ?,"
                " heartbeat_at = ? WHERE run_id = ? AND status = 'queued'",
                (at, at, run_id),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def heartbeat(self, run_id: str, *, processed_count: int,
                        at: str | None = None) -> None:
        """刷新心跳与进度(每题一次)。"""
        conn = await self._conn()
        try:
            await conn.execute(
                "UPDATE coverage_run SET heartbeat_at = ?, processed_count = ?"
                " WHERE run_id = ?",
                (at or now_iso(), int(processed_count), run_id),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def add_result(self, run_id: str, result: dict[str, Any], *,
                         processed_count: int, at: str | None = None) -> None:
        """落一题的结果 + 推进进度 + 刷心跳 —— 同一事务。

        题与进度分开写的话,崩在两者之间会让报告显示「已处理 N/总数」
        却缺一行结果;同一事务里两者要么都在要么都不在。
        """
        conn = await self._conn()
        try:
            await conn.execute(_INSERT_RESULT, _result_params(run_id, result))
            await conn.execute(
                "UPDATE coverage_run SET heartbeat_at = ?, processed_count = ?"
                " WHERE run_id = ?",
                (at or now_iso(), int(processed_count), run_id),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def append_skipped(self, run_id: str, seqs: list[int], *,
                             reason: str, at: str | None = None) -> int:
        """为未处理的题补 ``skipped`` 行(幂等:已有行不覆盖)。

        ``skipped`` 不是失败也不是零缺口 —— 它是「这一题没有被判定」,
        报告页据此才能对齐全题而不把缺席洗成任何一种结局。
        """
        if not seqs:
            return 0
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_RESULT_COLUMNS} FROM coverage_result WHERE run_id = ?",
                (run_id,),
            )
            existing = {int(r[1]) async for r in cursor}
            questions = await self._questions(conn, run_id)
            added = 0
            for seq in sorted(set(int(s) for s in seqs)):
                if seq in existing or seq >= len(questions):
                    continue
                result = {
                    "seq": seq,
                    "question": questions[seq],
                    "outcome": "skipped",
                    "outcome_reason": reason,
                }
                await conn.execute(_INSERT_RESULT, _result_params(run_id, result))
                added += 1
            await conn.execute(
                "UPDATE coverage_run SET processed_count ="
                " (SELECT COUNT(*) FROM coverage_result WHERE run_id = ?)"
                " WHERE run_id = ?",
                (run_id, run_id),
            )
            await conn.commit()
            return added
        finally:
            await conn.close()

    async def finish_run(
        self, run_id: str, *, status: str, summary: dict[str, Any] | None = None,
        error: str = "", at: str | None = None,
        processed_count: int | None = None,
    ) -> None:
        """写终态。终态行不再被心跳/进度更新搅动(条件化在 SQL 里)。"""
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"not a terminal status: {status!r}")
        conn = await self._conn()
        try:
            if processed_count is None:
                await conn.execute(
                    "UPDATE coverage_run SET status = ?, finished_at = ?,"
                    " error = ?, summary_json = ? WHERE run_id = ?",
                    (status, at or now_iso(), error,
                     _dumps(summary or {}), run_id),
                )
            else:
                await conn.execute(
                    "UPDATE coverage_run SET status = ?, finished_at = ?,"
                    " error = ?, summary_json = ?, processed_count = ?"
                    " WHERE run_id = ?",
                    (status, at or now_iso(), error, _dumps(summary or {}),
                     int(processed_count), run_id),
                )
            await conn.commit()
        finally:
            await conn.close()

    async def request_cancel(self, run_id: str) -> bool:
        """置取消标记;非活跃 run → False(调用方回 409)。"""
        conn = await self._conn()
        try:
            marks = ",".join("?" for _ in ACTIVE_STATUSES)
            cursor = await conn.execute(
                f"UPDATE coverage_run SET cancel_requested = 1"
                f" WHERE run_id = ? AND status IN ({marks})",
                (run_id, *ACTIVE_STATUSES),
            )
            await conn.commit()
            return cursor.rowcount > 0
        finally:
            await conn.close()

    # ── 读取 ─────────────────────────────────────────────

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM coverage_run WHERE run_id = ?",
                (run_id,),
            )
            row = await cursor.fetchone()
            return _row_to_run(row) if row is not None else None
        finally:
            await conn.close()

    async def active_run(self, datasource: str) -> dict[str, Any] | None:
        conn = await self._conn()
        try:
            marks = ",".join("?" for _ in ACTIVE_STATUSES)
            cursor = await conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM coverage_run"
                f" WHERE datasource = ? AND status IN ({marks})"
                f" ORDER BY created_at DESC LIMIT 1",
                (datasource, *ACTIVE_STATUSES),
            )
            row = await cursor.fetchone()
            return _row_to_run(row) if row is not None else None
        finally:
            await conn.close()

    async def list_runs(self, datasource: str, *, status: str | None = None,
                        limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        """按数据源的历史(新 → 旧;只含 run 行,不含逐题结果)。"""
        where = ["datasource = ?"]
        params: list[Any] = [datasource]
        if status:
            where.append("status = ?")
            params.append(status)
        params.extend([int(limit), int(offset)])
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM coverage_run"
                f" WHERE {' AND '.join(where)}"
                f" ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?",
                tuple(params),
            )
            return [_row_to_run(r) async for r in cursor]
        finally:
            await conn.close()

    async def get_results(self, run_id: str, *, seqs: list[int] | None = None,
                          ) -> list[dict[str, Any]]:
        conn = await self._conn()
        try:
            sql = (f"SELECT {_RESULT_COLUMNS} FROM coverage_result"
                   f" WHERE run_id = ?")
            params: list[Any] = [run_id]
            if seqs is not None:
                marks = ",".join("?" for _ in seqs)
                sql += f" AND seq IN ({marks})"
                params.extend(int(s) for s in seqs)
            sql += " ORDER BY seq"
            cursor = await conn.execute(sql, tuple(params))
            return [_row_to_result(r) async for r in cursor]
        finally:
            await conn.close()

    async def outcome_counts(self, run_id: str) -> dict[str, int]:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT outcome, COUNT(*) FROM coverage_result"
                " WHERE run_id = ? GROUP BY outcome",
                (run_id,),
            )
            return {str(r[0]): int(r[1]) async for r in cursor}
        finally:
            await conn.close()

    async def llm_calls_total(self, run_id: str) -> int:
        """已发生的模型调用总数(进度页「已用调用」;与硬上限同一把尺)。"""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT COALESCE(SUM(llm_calls), 0) FROM coverage_result"
                " WHERE run_id = ?",
                (run_id,),
            )
            row = await cursor.fetchone()
            return int(row[0] or 0) if row is not None else 0
        finally:
            await conn.close()

    async def done_seqs(self, run_id: str) -> set[int]:
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "SELECT seq FROM coverage_result WHERE run_id = ?", (run_id,))
            return {int(r[0]) async for r in cursor}
        finally:
            await conn.close()

    async def previous_done_run(self, datasource: str,
                                before_created_at: str) -> dict[str, Any] | None:
        """同源上一轮 ``done``(diff 的缺省基线)。"""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM coverage_run"
                f" WHERE datasource = ? AND status = 'done'"
                f" AND created_at < ? ORDER BY created_at DESC LIMIT 1",
                (datasource, before_created_at),
            )
            row = await cursor.fetchone()
            return _row_to_run(row) if row is not None else None
        finally:
            await conn.close()

    # ── 懒恢复 / 清退 ─────────────────────────────────────

    async def reconcile_stale(self, *, now: datetime | None = None,
                              stale_after_s: int = STALE_AFTER_S) -> list[str]:
        """心跳过期的活跃 run → ``interrupted``,未处理的题补 skipped。

        状态端点每次读之前都先调它 —— 崩溃留下的 ``running`` 行绝不能被
        报成「还在跑」。判断放在 Python 侧(活跃 run 至多几条):时间比较
        不依赖方言的日期函数,两后端同一行为。
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=stale_after_s)
        merged: list[str] = []
        for datasource in await self._active_datasources():
            active = await self.active_run(datasource)
            if active is None:
                continue
            beat = parse_iso(active.get("heartbeat_at") or active.get("created_at"))
            if beat is not None and beat > cutoff:
                continue
            at = now.isoformat()
            done = await self.done_seqs(active["run_id"])
            total = len(active.get("questions") or [])
            await self.append_skipped(
                active["run_id"], [s for s in range(total) if s not in done],
                reason="interrupted", at=at,
            )
            await self.finish_run(
                active["run_id"], status="interrupted", at=at,
                error="interrupted: heartbeat stale",
                processed_count=total,
            )
            logger.warning(
                "coverage run %s for %s marked interrupted (stale heartbeat)",
                active["run_id"], datasource,
            )
            merged.append(active["run_id"])
        return merged

    async def _active_datasources(self) -> list[str]:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT DISTINCT datasource FROM coverage_run"
                f" WHERE status IN ({marks})", ACTIVE_STATUSES)
            return [str(r[0]) async for r in cursor]
        finally:
            await conn.close()

    async def purge_old_runs(self, datasource: str, *,
                             keep: int = MAX_RUNS_PER_DATASOURCE) -> int:
        """只留最近 ``keep`` 条**终态** run;更老的连同 result 行清退。

        活跃 run 不在候选里(它们既不该被删,也不该占「最近 N 条」的名额)。
        先查 id 再删(跨方言 ``DELETE ... WHERE id IN (SELECT ...)`` 的写法
        在两个后端上都能用,但分两步让"删了哪些"可记录、可测试)。
        """
        marks = ",".join("?" for _ in TERMINAL_STATUSES)
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                f"SELECT run_id FROM coverage_run WHERE datasource = ?"
                f" AND status IN ({marks})"
                f" ORDER BY created_at DESC, run_id DESC",
                (datasource, *TERMINAL_STATUSES),
            )
            ids = [str(r[0]) async for r in cursor]
            doomed = ids[max(0, int(keep)):]
            for run_id in doomed:
                await conn.execute(
                    "DELETE FROM coverage_result WHERE run_id = ?", (run_id,))
                await conn.execute(
                    "DELETE FROM coverage_run WHERE run_id = ?", (run_id,))
            await conn.commit()
            return len(doomed)
        finally:
            await conn.close()

    async def delete_run(self, run_id: str) -> bool:
        """删一条 run 及其结果(测试与显式清理用;API 不暴露)。"""
        conn = await self._conn()
        try:
            await conn.execute(
                "DELETE FROM coverage_result WHERE run_id = ?", (run_id,))
            cursor = await conn.execute(
                "DELETE FROM coverage_run WHERE run_id = ?", (run_id,))
            await conn.commit()
            return cursor.rowcount > 0
        finally:
            await conn.close()

    @staticmethod
    async def _questions(conn, run_id: str) -> list[str]:
        cursor = await conn.execute(
            "SELECT questions_json FROM coverage_run WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return []
        return [str(q) for q in _load(row[0], [])]
