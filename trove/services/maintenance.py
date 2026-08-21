"""会话保留策略维护服务(配额清理 + checkpoint 级联)。

MaintenanceService 是确定性清理引擎:
  - sweep(): 每用户会话数超配额时,按 updated_at 升序删除最旧者,
             先删 checkpointer 的 thread_id 链,再 unlink 会话 db 文件。
  - (Task 4) purge_orphan_checkpoints / prune_thread_depth / run_all。

依赖注入 SessionStore + checkpointer(duck-typed,需 adelete_thread),
在 build_checkpointer 上下文内构造;测试传临时 home + fake checkpointer。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    """Parse an ISO timestamp from meta; None on malformed input."""
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


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

        # Group by user, oldest first within each group
        by_user: dict[str, list[dict]] = {}
        for s in all_sessions:
            by_user.setdefault(s["user_id"] or "unknown", []).append(s)
        for group in by_user.values():
            group.sort(key=lambda s: s["updated_at"])  # 升序:最旧在前

        for user, group in by_user.items():
            quota = self._retention.max_sessions_per_user
            candidates = group[: max(0, len(group) - quota)]
            for s in candidates:
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
                    stats.skipped_active += 1
                    continue
                await self._delete_session(s, stats)
        return stats

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
            import os
            from pathlib import Path

            db_path = Path(self._store.home_dir) / "sessions" / s["project_name"] / f"{s['session_id']}.db"
            if db_path.exists():
                stats.freed_bytes += db_path.stat().st_size
                db_path.unlink()
            stats.removed_sessions += 1
        except Exception as e:
            logger.warning("session file delete failed for %s: %s", s["session_id"], e)
            stats.errors += 1

    async def run_all(self, now: datetime | None = None) -> dict[str, Any]:
        """One-shot entry for daemon/lifespan: sweep (+ orphans, Task 4)."""
        return {"sweep": await self.sweep(now)}
