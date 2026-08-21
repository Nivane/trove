"""CLI job/schedule command smoke tests (real cwd, tmp SQLite)."""

from __future__ import annotations

import asyncio

import pytest

from trove.cli.schedule_cmds import main_job


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.asyncio
async def test_job_add_list_cancel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = _job_local({})

    await main_job(["add", "每月贷款总额是多少", "--interval", "60", "--alert", "row_count >= 5"])
    await main_job(["list"])
    jobs = await _jobs_in(tmp_path)
    assert len(jobs) == 1
    assert jobs[0].alert_expr == "row_count >= 5"
    assert jobs[0].next_run_at  # interval → valid schedule

    await main_job(["cancel", jobs[0].id])
    assert await _jobs_in(tmp_path) == []


@pytest.mark.asyncio
async def test_job_add_cron(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    await main_job(["add", "每日汇总", "--cron", "0 9 * * *"])
    jobs = await _jobs_in(tmp_path)
    assert jobs[0].schedule_type == "cron"
    assert jobs[0].schedule == "0 9 * * *"


@pytest.mark.asyncio
async def test_job_add_invalid_schedule_exits(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        await main_job(["add", "q", "--cron", "99 99 99 99 99"])
    out = capsys.readouterr().out
    assert "invalid schedule" in out


@pytest.mark.asyncio
async def test_list_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    await main_job(["list"])
    assert "No scheduled jobs." in capsys.readouterr().out


async def _jobs_in(tmp_path):
    from trove.services.jobs.store import JobStore

    return await JobStore(tmp_path).load_jobs()


def _job_local(state):
    return state


def test_should_sweep():
    """周期判断:间隔内不触发,超过触发,interval<=0 关闭。"""
    from trove.cli.schedule_cmds import should_sweep

    now = 1_000_000.0
    assert should_sweep(now, now, 24) is False
    assert should_sweep(now, now + 24 * 3600 - 1, 24) is False
    assert should_sweep(now, now + 24 * 3600, 24) is True
    assert should_sweep(0.0, now, 24) is True  # 从未 sweep
    assert should_sweep(now, now + 1_000_000, 0) is False  # 关闭
    assert should_sweep(now, now + 1_000_000, -5) is False