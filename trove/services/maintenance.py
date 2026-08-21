"""会话保留策略维护服务(配额清理 + checkpoint 级联)。

MaintenanceService 是确定性清理引擎:
  - sweep(): 每用户会话数超配额时,按 updated_at 升序删除最旧者,
             先删 checkpointer 的 thread_id 链,再 unlink 会话 db 文件。
  - (Task 4) purge_orphan_checkpoints / prune_thread_depth / run_all。

依赖注入 SessionStore + checkpointer(duck-typed,需 adelete_thread),
在 build_checkpointer 上下文内构造;测试传临时 home + fake checkpointer。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trove.core.config import RetentionConfig
from trove.core.logging import get_logger
from trove.storage.session_store import SessionStore

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SweepStats:
    """一次 sweep 的统计结果(供 CLI/lifespan 打印)。"""

    scanned: int = 0
    removed_sessions: int = 0
    removed_checkpoints: int = 0
    freed_bytes: int = 0
    errors: int = 0
    skipped_active: int = 0

    def __str__(self) -> str:
        return (
            f"scanned={self.scanned} removed={self.removed_sessions} "
            f"checkpoints={self.removed_checkpoints} freed={self.freed_bytes}B "
            f"skipped_active={self.skipped_active} errors={self.errors}"
        )


def _parse_dt(value: str) -> datetime | None:
    """Parse an ISO timestamp from meta; None on malformed or naive input."""
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None  # naive datetime = 脏数据,回退文件 mtime
    return dt


def _is_active(updated_at: str, file_mtime: float, grace_min: int, now: datetime) -> bool:
    """True when the session was touched within the grace window.

    Falls back to the file's mtime when meta.updated_at is missing/malformed.
    """
    if grace_min <= 0:
        return False
    cutoff = now - timedelta(minutes=grace_min)
    ts = _parse_dt(updated_at)
    if ts is None:
        return datetime.fromtimestamp(file_mtime, tz=timezone.utc) > cutoff
    return ts > cutoff


def _is_active_checkpoint(metadata: dict, grace_min: int, now: datetime) -> bool:
    """True when a checkpoint's metadata carries a fresh timestamp.

    No timestamp / unparseable -> False (never exempts). Real langgraph
    CheckpointMetadata (langgraph-checkpoint 4.2.0, vendored with
    langgraph 1.2.11) has NO timestamp field and trove's graph config
    passes none, so the grace protection only kicks in when a caller
    embeds ``updated_at``/``timestamp`` in checkpoint metadata.
    """
    if grace_min <= 0:
        return False
    raw = metadata.get("updated_at") or metadata.get("timestamp")
    ts = _parse_dt(raw) if isinstance(raw, str) else None
    if ts is None:
        return False
    return ts > now - timedelta(minutes=grace_min)


def _group_by_user(sessions: list[dict]) -> dict[str, list[dict]]:
    """Group sessions by user (None/"" -> "unknown"), oldest first per group."""
    by_user: dict[str, list[dict]] = {}
    for s in sessions:
        by_user.setdefault(s["user_id"] or "unknown", []).append(s)
    for group in by_user.values():
        group.sort(key=lambda s: s["updated_at"])  # 升序:最旧在前
    return by_user


class MaintenanceService:
    """Retention enforcement over sessions and graph checkpoints."""

    def __init__(
        self,
        session_store: SessionStore,
        checkpointer,
        retention: RetentionConfig,
    ):
        self._store = session_store
        self._ckpt = checkpointer
        self._retention = retention

    async def sweep(self, now: datetime | None = None) -> SweepStats:
        """Enforce the per-user session quota; returns sweep stats."""
        now = now or _utcnow()
        stats = SweepStats()
        if self._retention.max_sessions_per_user <= 0:
            return stats  # 配额清理关闭

        all_sessions = await self._store.list_all()
        stats.scanned = len(all_sessions)

        for user, (candidates, skipped) in self._quota_candidates(
            _group_by_user(all_sessions), now
        ).items():
            stats.skipped_active += skipped
            for s in candidates:
                await self._delete_session(s, stats)
        return stats

    def _quota_candidates(
        self,
        by_user: dict[str, list[dict]],
        now: datetime,
    ) -> dict[str, tuple[list[dict], int]]:
        """Per-user quota-excess candidates + active-skips (shared口径).

        Shared by sweep() and preview(): excess over the per-user quota,
        oldest first, `_is_active` grace exemption applied. A kept entry
        is a real deletion candidate; a skipped one counts toward the
        user's skip tally.
        """
        quota = self._retention.max_sessions_per_user
        plan: dict[str, tuple[list[dict], int]] = {}
        if quota <= 0:
            return plan
        for user, group in by_user.items():
            over = group[: max(0, len(group) - quota)]
            candidates: list[dict] = []
            skipped = 0
            for s in over:
                db_path = (
                    Path(self._store.home_dir)
                    / "sessions"
                    / s["project_name"]
                    / f"{s['session_id']}.db"
                )
                file_mtime = db_path.stat().st_mtime if db_path.exists() else 0.0
                if _is_active(
                    s["updated_at"],
                    file_mtime,
                    self._retention.active_grace_min,
                    now,
                ):
                    skipped += 1
                else:
                    candidates.append(s)
            plan[user] = (candidates, skipped)
        return plan

    async def preview(self, now: datetime | None = None) -> dict[str, Any]:
        """Dry-run view of the quota sweep; deletes nothing.

        Same candidate口径 as sweep() (per-user quota excess, oldest
        first, active-grace exemption) via the shared `_quota_candidates`.
        Returns per-user counts so callers can total or display detail.
        """
        now = now or _utcnow()
        all_sessions = await self._store.list_all()
        plan = self._quota_candidates(_group_by_user(all_sessions), now)
        return {
            "sessions": len(all_sessions),
            "candidates": {user: len(c) for user, (c, _s) in plan.items() if c},
            "skipped_active": {user: s for user, (_c, s) in plan.items() if s},
        }

    async def _delete_session(self, s: dict, stats: SweepStats) -> None:
        """Delete one session: checkpoint chain first, then the db file."""
        try:
            await self._ckpt.adelete_thread(s["session_id"])
            stats.removed_checkpoints += 1
        except Exception as e:
            logger.warning("checkpoint delete failed for %s: %s", s["session_id"], e)
            stats.errors += 1
            return  # 文件保留,避免"无 checkpoint 有文件"的半删状态
        try:
            db_path = Path(self._store.home_dir) / "sessions" / s["project_name"] / f"{s['session_id']}.db"
            if db_path.exists():
                stats.freed_bytes += db_path.stat().st_size
                db_path.unlink()
            stats.removed_sessions += 1
        except Exception as e:
            logger.warning("session file delete failed for %s: %s", s["session_id"], e)
            stats.errors += 1

    async def purge_orphan_checkpoints(self) -> int:
        """Delete checkpoint threads with no matching session file.

        Fixes the historical accumulation: sessions deleted before
        cascade logic existed left orphan threads in checkpoints.db.

        Deletion and counting are at thread level: the thread_id set is
        deduplicated from a streaming alist pass (no full checkpoint rows
        materialized), so a thread with N rows is deleted once and counts
        once.
        """
        removed = 0
        try:
            all_sessions = await self._store.list_all()
            existing = {s["session_id"] for s in all_sessions}
            # stream every thread in the shared checkpoint db; only the
            # thread_id is kept (this langgraph version yields
            # CheckpointTuple objects, thread_id lives in
            # t.config["configurable"]["thread_id"])
            orphan_threads: set[str] = set()
            async for t in self._ckpt.alist(None):
                thread_id = t.config["configurable"]["thread_id"]
                if thread_id not in existing:
                    orphan_threads.add(thread_id)
            for thread_id in orphan_threads:
                try:
                    await self._ckpt.adelete_thread(thread_id)
                    removed += 1
                except Exception as e:
                    logger.warning("orphan checkpoint delete failed %s: %s", thread_id, e)
        except Exception as e:
            logger.warning("purge_orphan_checkpoints failed: %s", e)
        return removed

    async def prune_thread_depth(self, depth: int | None = None) -> int:
        """Cap checkpoint rows per thread; returns number of threads pruned.

        ``depth=None`` falls back to the retention config; an explicit
        ``depth=0`` disables pruning (no fallback).

        Threads whose newest checkpoint carries a timestamp inside the
        active-grace window are skipped (in-flight conversation protection);
        metadata without a timestamp is never exempted.
        """
        depth = self._retention.max_checkpoints_per_thread if depth is None else depth
        if depth <= 0:
            return 0
        pruned = 0
        now = _utcnow()
        try:
            counts: dict[str, int] = {}
            # streaming pass: only the thread_id is kept per row
            async for t in self._ckpt.alist(None):
                thread_id = t.config["configurable"]["thread_id"]
                counts[thread_id] = counts.get(thread_id, 0) + 1
            for thread_id, n in counts.items():
                if n > depth:
                    try:
                        if await self._thread_active(thread_id, now):
                            continue  # 最新 checkpoint 在 grace 窗口内 → 豁免
                        await self._prune_thread(thread_id, depth)
                        pruned += 1
                    except Exception as e:
                        logger.warning("prune failed for %s: %s", thread_id, e)
        except Exception as e:
            logger.warning("prune_thread_depth failed: %s", e)
        return pruned

    async def _thread_active(self, thread_id: str, now: datetime) -> bool:
        """True when the thread's newest checkpoint is inside the grace window."""
        latest = [
            t
            async for t in self._ckpt.alist(
                {"configurable": {"thread_id": thread_id}}, limit=1
            )
        ]
        if not latest:
            return False
        return _is_active_checkpoint(
            latest[0].metadata or {}, self._retention.active_grace_min, now
        )

    async def _prune_thread(self, thread_id: str, depth: int) -> None:
        """Rewrite a thread keeping only its newest ``depth`` checkpoints.

        The locked langgraph-checkpoint-sqlite (3.1.1) ships ``aprune`` as a
        NotImplementedError stub, so the depth cap is done with the primitives
        that do work here: alist(limit=depth) -> adelete_thread -> re-put.

        Parent pointers are preserved: each kept row is re-put with its
        original parent's checkpoint_id, so only the oldest kept row's parent
        (inside the deleted segment) is a dangling pointer and the chain
        naturally terminates there. Pending-writes rows are dropped (same
        DeltaChannel caveat the langgraph docs attach to prune).

        Interruption semantics: the rewrite is two-phase (whole-thread
        delete, then per-row re-put) and therefore not atomic — a crash
        mid-rewrite truncates the thread, a wider window than the official
        prune's single DELETE. Accepted: maintenance runs inside the serve
        process alongside graph executions (same-process concurrency) and
        skips threads whose newest checkpoint is within the grace window;
        a crash loses only the deleted segment's older history.
        """
        kept = [
            t
            async for t in self._ckpt.alist(
                {"configurable": {"thread_id": thread_id}}, limit=depth
            )
        ]
        if not kept:
            return
        await self._ckpt.adelete_thread(thread_id)
        for t in kept:
            # re-put with the ORIGINAL parent pointer (config's
            # checkpoint_id = the row's parent), not the row's own id
            cfg = {"configurable": dict(t.config["configurable"])}
            parent_cid = None
            if t.parent_config is not None:
                parent_cid = t.parent_config.get("configurable", {}).get("checkpoint_id")
            if parent_cid is not None:
                cfg["configurable"]["checkpoint_id"] = parent_cid
            else:
                cfg["configurable"].pop("checkpoint_id", None)
            await self._ckpt.aput(cfg, t.checkpoint, t.metadata, {})

    async def run_all(self, now: datetime | None = None) -> dict[str, Any]:
        """One-shot entry for daemon/lifespan: orphans -> depth -> sweep.

        Each stage is isolated: a failure in one never blocks the others.
        """
        orphans = await self.purge_orphan_checkpoints()
        pruned = await self.prune_thread_depth()
        try:
            sweep = await self.sweep(now)
        except Exception as e:
            logger.warning("sweep failed in run_all: %s", e)
            sweep = SweepStats(errors=1)
        return {"orphans": orphans, "pruned": pruned, "sweep": sweep}
