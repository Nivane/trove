"""CLI commands for scheduled jobs & the scheduler daemon.

  trove-cli job add "每月贷款总额" --interval 1440 --alert "row_count >= 5" --channel console
  trove-cli job add "每日贷款总量" --cron "0 9 * * *"
  trove-cli job list | cancel <id>
  trove-cli schedule --once                 # run all due jobs once
  trove-cli schedule --daemon               # poll + run (Ctrl-C to stop)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.services.jobs.service import JobsService
from trove.services.jobs.store import JobStore

logger = get_logger(__name__)


def should_sweep(last_sweep: float, now: float, interval_hours: int) -> bool:
    """True when a periodic maintenance sweep is due (interval<=0 = off)."""
    if interval_hours <= 0:
        return False
    return (now - last_sweep) >= interval_hours * 3600


def _args_for(cmd: str, datasource: str | None = None):
    """Shared args consumed by _load_config / create_app_components."""
    import types

    return types.SimpleNamespace(
        datasource=datasource or "demo", config=None, model=None, _cmd=cmd,
    )


async def _load_config_with(args) -> AgentConfig:
    from trove.main import _load_config

    return await _load_config(args)


def _jobs_service() -> JobsService:
    return JobsService(JobStore(Path.cwd()))


def _job_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trove job", description="Scheduled jobs")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    add = sub.add_parser("add", help="Register a new scheduled question")
    add.add_argument("question", help="The question to run on schedule")
    add.add_argument("--name", default="", help="Job display name (default: question prefix)")
    add.add_argument("--interval", type=int, default=0, help="Repeat interval in minutes")
    add.add_argument("--cron", default="", help='Cron expression, e.g. "0 9 * * *"')
    add.add_argument("--datasource", default="demo", help="Datasource (default demo)")
    add.add_argument("--workflow", default="reflection", help="reflection|fixed")
    add.add_argument("--alert", default="", help='Alert rule, e.g. "row_count >= 5"')
    add.add_argument("--channel", default="console", help="console | webhook:<url>")
    add.add_argument("--cooldown", type=int, default=30, help="Alert cooldown minutes")

    add.add_argument("--list", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("list", help="List scheduled jobs")
    cancel = sub.add_parser("cancel", help="Delete a job")
    cancel.add_argument("job_id")
    return parser


async def main_job(argv: list[str]) -> None:
    parser = _job_parser()
    args = parser.parse_args(argv)
    jobs = _jobs_service()

    if args.subcommand == "add":
        schedule_type = "interval" if (args.cron or "") == "" and args.interval else (
            "cron" if args.cron else "interval"
        )
        schedule = args.cron if args.cron else str(args.interval or 0)
        if schedule_type == "interval" and int(args.interval or 0) < 1:
            print("add: --interval >= 1 or provide --cron")
            sys.exit(1)
        job = await jobs.create_job(
            args.question,
            schedule,
            schedule_type,
            name=args.name,
            datasource=args.datasource,
            workflow=args.workflow,
            alert_expr=args.alert,
            alert_channel=args.channel,
            alert_cooldown_min=args.cooldown,
        )
        if job is None:
            print(f"add: invalid schedule {schedule!r} ({schedule_type})")
            sys.exit(1)
        print(f"Job {job.id} created — next run {job.next_run_at}")
        print(json.dumps({
            "id": job.id, "name": job.name, "question": job.question,
            "schedule_type": job.schedule_type, "schedule": job.schedule,
            "next_run_at": job.next_run_at,
            "alert_expr": job.alert_expr, "alert_channel": job.alert_channel,
        }, ensure_ascii=False, indent=2))
        return

    if args.subcommand == "list":
        jobs_list = await jobs.list_jobs()
        if not jobs_list:
            print("No scheduled jobs.")
            return
        for j in jobs_list:
            enabled = "enabled" if j.enabled else "disabled"
            alert = f", alert={j.alert_expr}" if j.alert_expr else ""
            print(
                f"[{j.id}] {j.name} | {j.schedule_type}:{j.schedule} "
                f"| {j.question[:48]} | {enabled}{alert} | next {j.next_run_at}"
            )
        return

    if args.subcommand == "cancel":
        ok = await jobs.cancel(args.job_id)
        print(f"Cancelled {args.job_id}" if ok else f"No job {args.job_id}")
        if not ok:
            sys.exit(1)
        return

    parser.print_help()


def _schedule_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trove schedule", description="Run due scheduled jobs")
    parser.add_argument("--once", action="store_true", help="Run due jobs once and exit")
    parser.add_argument("--daemon", action="store_true", help="Loop forever (default)")
    parser.add_argument("--poll-seconds", type=int, default=30, help="Daemon poll interval")
    parser.add_argument("--datasource", default="demo", help="Datasource")
    parser.add_argument("--config", default=None, help="agent.yml path")
    parser.add_argument("--model", default=None, help="LLM model override")
    return parser


async def main_schedule(argv: list[str]) -> None:
    args = _schedule_parser().parse_args(argv)
    from trove.main import build_checkpointer, create_app_components

    config = await _load_config_with(args)
    async with build_checkpointer(config.home) as checkpointer:
        components = await create_app_components(args, config, checkpointer)
        session_manager = components["session_manager"]
        jobs = _jobs_service()
        from trove.services.jobs.runner import SchedulerRunner

        runner = SchedulerRunner(session_manager, jobs)
        try:
            if args.once:
                results = await runner.tick()
                if not results:
                    print("No jobs due.")
                for r in results:
                    print(json.dumps(r, ensure_ascii=False))
                return
            import time as _time

            from trove.services.maintenance import MaintenanceService
            from trove.storage.session_store import SessionStore

            maintenance = MaintenanceService(
                SessionStore(home_dir=config.home),
                checkpointer,
                config.retention,
            )
            print(
                "Scheduler daemon running "
                f"(poll every {args.poll_seconds}s; Ctrl-C to stop)"
            )
            last_sweep = 0.0
            while True:
                results = await runner.tick()
                for r in results:
                    print(json.dumps(r, ensure_ascii=False), flush=True)
                if should_sweep(last_sweep, _time.time(), config.retention.sweep_interval_hours):
                    try:
                        stats = await maintenance.run_all()
                        print(f"[maintenance] {stats}", flush=True)
                    except Exception as e:
                        logger.warning("maintenance sweep failed: %s", e)
                    last_sweep = _time.time()
                await asyncio.sleep(args.poll_seconds)
        finally:
            await components["connector_registry"].close_all()