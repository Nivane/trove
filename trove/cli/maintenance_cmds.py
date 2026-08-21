"""CLI commands for session retention maintenance.

  trove maintenance status                     # 会话数/磁盘占用/配额报告
  trove maintenance run                        # 配额清理 + 深度修剪(不含孤儿清理)
  trove maintenance run --dry-run              # 只报告候选,不删除
  trove maintenance run --purge-orphans        # 额外执行孤儿 checkpoint 清理(默认不含)
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from trove.core.config import RetentionConfig
from trove.core.logging import get_logger
from trove.services.maintenance import MaintenanceService, SweepStats

logger = get_logger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trove maintenance", description="Session retention maintenance")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    run = sub.add_parser("run", help="Run retention sweep (quota cleanup + checkpoint depth pruning)")
    run.add_argument("--dry-run", action="store_true", help="Report only; delete nothing")
    run.add_argument("--purge-orphans", action="store_true", help="Also purge orphan checkpoints (not included by default)")
    sub.add_parser("status", help="Report session counts / disk usage vs quota")
    return parser


async def _run_maintenance(home: str, config, dry_run: bool, purge_orphans: bool) -> dict:
    """Execute the retention pass; returns stats dict.

    Default pass = quota sweep + checkpoint depth pruning; orphan
    checkpoint cleanup only runs with ``--purge-orphans`` (the
    ``orphans`` key is kept at 0 so downstream parsers see a stable shape).
    """
    from trove.main import build_checkpointer
    from trove.storage.session_store import SessionStore

    store = SessionStore(home_dir=home)
    async with build_checkpointer(home) as checkpointer:
        svc = MaintenanceService(store, checkpointer, config.retention)
        if dry_run:
            # Dry run: report candidates without deleting.
            all_sessions = await store.list_all()
            by_user: dict[str, int] = {}
            for s in all_sessions:
                key = s["user_id"] or "unknown"
                by_user[key] = by_user.get(key, 0) + 1
            quota = config.retention.max_sessions_per_user
            candidates = sum(max(0, n - quota) for n in by_user.values()) if quota > 0 else 0
            return {"dry_run": True, "sessions": len(all_sessions), "candidates": candidates}
        if purge_orphans:
            return await svc.run_all()
        # 默认:配额 sweep + 深度修剪,不含孤儿清理;与 run_all 同款错误隔离
        pruned = await svc.prune_thread_depth()
        try:
            sweep = await svc.sweep()
        except Exception as e:
            logger.warning("sweep failed in maintenance run: %s", e)
            sweep = SweepStats(errors=1)
        return {"orphans": 0, "pruned": pruned, "sweep": sweep}


async def main_maintenance(argv: list[str]) -> None:
    args = _parser().parse_args(argv)
    import types
    from trove.main import _load_config

    cfg_args = types.SimpleNamespace(datasource="demo", config=None, model=None, _cmd="maintenance")
    config = await _load_config(cfg_args)

    if args.subcommand == "status":
        from trove.storage.session_store import SessionStore
        store = SessionStore(home_dir=config.home)
        sessions = await store.list_all()
        by_user: dict[str, int] = {}
        size_bytes = 0
        for s in sessions:
            key = s["user_id"] or "unknown"
            by_user[key] = by_user.get(key, 0) + 1
            size_bytes += int(s.get("size_bytes") or 0)
        quota = config.retention.max_sessions_per_user
        print(
            f"sessions={len(sessions)} quota_per_user={quota} "
            f"disk_mb={size_bytes / (1024 * 1024):.1f}"
        )
        for user, n in sorted(by_user.items()):
            print(f"  {user}: {n}")
        return

    if args.subcommand == "run":
        stats = await _run_maintenance(
            config.home, config, dry_run=args.dry_run, purge_orphans=args.purge_orphans
        )
        import json
        print(json.dumps(stats, ensure_ascii=False, default=str))
        return

    _parser().print_help()
