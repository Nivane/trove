"""CLI commands for session retention maintenance.

  trove maintenance status                     # 会话数/磁盘占用/配额报告
  trove maintenance run                        # 配额清理 + 深度修剪(不含孤儿清理)
  trove maintenance run --dry-run              # 只报告候选,不删除
  trove maintenance run --purge-orphans        # 额外执行孤儿 checkpoint 清理(默认不含)
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from trove.core.logging import get_logger
from trove.services.maintenance import MaintenanceService, SweepStats

logger = get_logger(__name__)

#: A cutoff no stored ``started_at`` can reach (used only for status totals;
#: ``count_before`` is the same query the purge leg uses).
_HORIZON = "9999-12-31T23:59:59+00:00"


async def _run_record_retention(home: str, config, *, dry_run: bool) -> dict:
    """Request-record retention leg of ``maintenance run``.

    ``run_record_retention_days <= 0`` = 永久保留 (skip). ``has_data()``
    avoids materializing an empty database for deployments that never
    recorded anything. The store is disposed in a ``finally``: these CLI
    commands are one-shot processes and a live aiosqlite worker keeps them
    from exiting.
    """
    from trove.storage.run_record_store import RunRecordStore

    days = int(getattr(config.retention, "run_record_retention_days", 0) or 0)
    store = RunRecordStore(home)
    try:
        if days <= 0:
            return {"skipped": "retention_disabled"}
        if not await store.has_data():
            return {"runs": 0, "events": 0}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
            timespec="seconds")
        if dry_run:
            return await store.count_before(cutoff)
        return await store.purge_before(cutoff)
    finally:
        await store.dispose()


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
            # Dry run: same candidate口径 as the real sweep (per-user quota
            # excess, oldest first, active-grace exemption) via the
            # service's preview(), so dry-run and run can never diverge.
            preview = await svc.preview()
            return {
                "dry_run": True,
                "sessions": preview["sessions"],
                "candidates": sum(preview["candidates"].values()),
                "skipped_active": sum(preview["skipped_active"].values()),
                "run_records": await _run_record_retention(
                    home, config, dry_run=True),
            }
        if purge_orphans:
            stats = await svc.run_all()
        else:
            # 默认:配额 sweep + 深度修剪,不含孤儿清理;与 run_all 同款错误隔离
            pruned = await svc.prune_thread_depth()
            try:
                sweep = await svc.sweep()
            except Exception as e:
                logger.warning("sweep failed in maintenance run: %s", e)
                sweep = SweepStats(errors=1)
            stats = {"orphans": 0, "pruned": pruned, "sweep": sweep}
        stats["run_records"] = await _run_record_retention(
            home, config, dry_run=False)
        return stats


async def main_maintenance(argv: list[str]) -> None:
    args = _parser().parse_args(argv)
    import types
    from trove.main import _load_config

    cfg_args = types.SimpleNamespace(datasource="demo", config=None, model=None, _cmd="maintenance")
    config = await _load_config(cfg_args)

    if args.subcommand == "status":
        from trove.services.maintenance import _group_by_user
        from trove.storage.session_store import SessionStore
        store = SessionStore(home_dir=config.home)
        sessions = await store.list_all()
        by_user = _group_by_user(sessions)
        size_bytes = 0
        for s in sessions:
            size_bytes += int(s.get("size_bytes") or 0)
        quota = config.retention.max_sessions_per_user
        print(
            f"sessions={len(sessions)} quota_per_user={quota} "
            f"disk_mb={size_bytes / (1024 * 1024):.1f}"
        )
        for user, group in sorted(by_user.items()):
            print(f"  {user}: {len(group)}")
        # 请求记录:只报数,不建库(没录过的部署不留空文件)。
        from trove.storage.run_record_store import RunRecordStore
        run_records = RunRecordStore(config.home)
        try:
            totals = (
                await run_records.count_before(_HORIZON)
                if await run_records.has_data() else {"runs": 0, "events": 0}
            )
        finally:
            await run_records.dispose()
        days = config.retention.run_record_retention_days
        print(
            f"run_records={totals['runs']} events={totals['events']} "
            f"retention_days={days}{' (permanent)' if days <= 0 else ''}"
        )
        return

    if args.subcommand == "run":
        stats = await _run_maintenance(
            config.home, config, dry_run=args.dry_run, purge_orphans=args.purge_orphans
        )
        import json
        print(json.dumps(stats, ensure_ascii=False, default=str))
        return

    _parser().print_help()
