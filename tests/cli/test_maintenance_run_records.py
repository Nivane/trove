"""``trove maintenance`` run-record legs: status line, dry-run, real purge.

Same shape as the session-quota tests: point ``config.home`` at tmp through
``conf/agent.yml`` and drive the real CLI entry point. The store must be
disposed even on the skip paths — these are one-shot processes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trove.cli.maintenance_cmds import (
    _run_record_retention,
    main_maintenance,
)
from trove.storage.run_record_store import DB_FILE_NAME, RunRecordStore


def _point_home(
    monkeypatch, tmp_path: Path, *, run_record_days: int | None = None,
) -> Path:
    home = tmp_path / ".trove"
    conf = tmp_path / "conf"
    conf.mkdir(parents=True, exist_ok=True)
    retention = "    max_sessions_per_user: 100\n    active_grace_min: 10\n"
    if run_record_days is not None:
        retention += f"    run_record_retention_days: {run_record_days}\n"
    (conf / "agent.yml").write_text(
        f"agent:\n  home: {home}\n  retention:\n{retention}",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return home


def _config(days: int):
    return SimpleNamespace(
        retention=SimpleNamespace(run_record_retention_days=days))


async def _seed(home: Path) -> None:
    store = RunRecordStore(home)
    try:
        await store.write(
            {"run_id": "old", "started_at": "2020-01-01T00:00:00+00:00",
             "status": "done"},
            [{"seq": 0, "kind": "run", "payload": {}}],
        )
        await store.write(
            {"run_id": "new", "started_at": "2099-01-01T00:00:00+00:00",
             "status": "done"},
            [{"seq": 0, "kind": "run", "payload": {}}],
        )
    finally:
        await store.dispose()


async def test_disabled_skips_and_creates_no_db(tmp_path):
    result = await _run_record_retention(
        str(tmp_path), _config(0), dry_run=False)
    assert result == {"skipped": "retention_disabled"}
    assert not (tmp_path / DB_FILE_NAME).exists()


async def test_unused_home_reports_zero_without_db(tmp_path):
    result = await _run_record_retention(
        str(tmp_path), _config(30), dry_run=False)
    assert result == {"runs": 0, "events": 0}
    assert not (tmp_path / DB_FILE_NAME).exists()


async def test_dry_run_counts_without_deleting(tmp_path):
    await _seed(tmp_path)
    result = await _run_record_retention(
        str(tmp_path), _config(30), dry_run=True)
    assert result == {"runs": 1, "events": 1}
    store = RunRecordStore(tmp_path)
    try:
        assert await store.get("old") is not None
    finally:
        await store.dispose()


async def test_real_run_purges_old_only(tmp_path):
    await _seed(tmp_path)
    result = await _run_record_retention(
        str(tmp_path), _config(30), dry_run=False)
    assert result == {"runs": 1, "events": 1}
    store = RunRecordStore(tmp_path)
    try:
        assert await store.get("old") is None
        assert await store.get("new") is not None
    finally:
        await store.dispose()


@pytest.mark.asyncio
async def test_status_prints_run_record_line(tmp_path, monkeypatch, capsys):
    home = _point_home(monkeypatch, tmp_path, run_record_days=0)
    await _seed(home)
    await main_maintenance(["status"])
    out = capsys.readouterr().out
    assert "run_records=2 events=2 retention_days=0 (permanent)" in out


@pytest.mark.asyncio
async def test_run_json_carries_the_leg(tmp_path, monkeypatch, capsys):
    _point_home(monkeypatch, tmp_path)
    await main_maintenance(["run", "--dry-run"])
    out = capsys.readouterr().out
    assert '"run_records"' in out
    assert '"skipped": "retention_disabled"' not in out
