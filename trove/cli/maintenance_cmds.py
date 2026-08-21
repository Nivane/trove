"""CLI commands for session retention maintenance.

  trove maintenance status                     # 会话数/磁盘占用/配额报告
  trove maintenance run                        # 执行配额清理 + 深度修剪 + 孤儿清理
  trove maintenance run --dry-run              # 只报告候选,不删除
  trove maintenance run --purge-orphans        # 强制含孤儿清理(默认已含)
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from trove.core.config import RetentionConfig
from trove.core.logging import get_logger
from trove.services.maintenance import MaintenanceService

logger = get_logger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trove maintenance", description="Session retention maintenance")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    run = sub.add_parser("run", help="Run retention sweep (quota cleanup + checkpoint hygiene)")
    run.add_argument("--dry-run", action="store_true", help="Report only; delete nothing")
    run.add_argument("--purge-orphans", action="store_true", help="Also purge orphan checkpoints (included by default)")
    sub.add_parser("status", help="Report session counts / disk usage vs quota")
    return parser


async def _run_maintenance(home: str, config, dry_run: bool) -> dict:
    """Execute the full retention pass; returns stats dict."""
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
        return await svc.run_all()


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
        for s in sessions:
            key = s["user_id"] or "unknown"
            by_user[key] = by_user.get(key, 0) + 1
        quota = config.retention.max_sessions_per_user
        print(f"sessions={len(sessions)} quota_per_user={quota}")
        for user, n in sorted(by_user.items()):
            print(f"  {user}: {n}")
        return

    if args.subcommand == "run":
        stats = await _run_maintenance(config.home, config, dry_run=args.dry_run)
        import json
        print(json.dumps(stats, ensure_ascii=False, default=str))
        return

    _parser().print_help()
