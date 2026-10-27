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


def _write_model(tmp_path, datasource="demo"):
    """cwd 根下写一份带 loans 域的最小语义模型(与 runner 读的同一份)。"""
    from tests.helpers.kb import topic_model_yaml

    path = tmp_path / ".trove" / "kb" / datasource / "semantics.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        topic_model_yaml(["loan", "account"], {"loans": ["loan", "account"]}),
        encoding="utf-8")


@pytest.mark.asyncio
async def test_job_add_topic_passthrough(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_model(tmp_path)
    await main_job(["add", "每月贷款总额是多少", "--interval", "60",
                    "--topic", "loans"])
    jobs = await _jobs_in(tmp_path)
    assert jobs[0].topic == "loans"
    assert '"topic": "loans"' in capsys.readouterr().out

    await main_job(["list"])
    assert "topic=loans" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_job_add_unknown_topic_exits(tmp_path, monkeypatch, capsys):
    """写坏的名字会以同样的方式失败到永远 —— 建任务时就拦下。"""
    monkeypatch.chdir(tmp_path)
    _write_model(tmp_path)
    with pytest.raises(SystemExit):
        await main_job(["add", "q", "--interval", "60", "--topic", "nope"])
    out = capsys.readouterr().out
    assert "unknown topic" in out
    assert "loans" in out  # 说清声明里有什么
    assert await _jobs_in(tmp_path) == []


@pytest.mark.asyncio
async def test_job_add_topic_without_semantic_model_exits(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        await main_job(["add", "q", "--interval", "60", "--topic", "loans"])
    assert "no semantic model" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_job_add_without_topic_needs_no_model(tmp_path, monkeypatch):
    """不带主题域的任务对语义模型零依赖(既有行为不变)。"""
    monkeypatch.chdir(tmp_path)
    await main_job(["add", "q", "--interval", "60"])
    jobs = await _jobs_in(tmp_path)
    assert jobs[0].topic == ""


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


@pytest.mark.asyncio
async def test_daemon_runner_gets_the_shared_components(tmp_path, monkeypatch):
    """``schedule --daemon`` 的 runner 必须拿到 components 里的五件注入。

    历史缺陷:这个构造点漏了 decision / verdicts / actions / subscriptions,
    daemon 于是"看着在跑"但决策任务只能报未接线、触发型规则静默不提案。这里
    用一个假 components + 假 SchedulerRunner 跑一遍 ``--once``,断言注入的
    正是**同一个对象**(不是就地 new 出来的替身)。
    """
    from trove.cli import schedule_cmds

    class _Registry:
        async def close_all(self):
            return None

    session_manager = object()
    components = {
        "session_manager": session_manager,
        "connector_registry": _Registry(),
        "decision": object(),
        "verdicts": object(),
        "actions": object(),
        "subscriptions": object(),
        "scans": object(),
    }
    captured: dict = {}

    class _FakeRunner:
        def __init__(self, sm, jobs, **kw):
            captured["sm"] = sm
            captured.update(kw)

        async def tick(self):
            return []

    class _FakeCheckpointer:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def fake_load_config(args):
        from trove.core.config import AgentConfig

        return AgentConfig()

    async def fake_create(args, config, checkpointer):
        return components

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(schedule_cmds, "_load_config_with", fake_load_config)
    monkeypatch.setattr("trove.main.build_checkpointer",
                        lambda home: _FakeCheckpointer())
    monkeypatch.setattr("trove.main.create_app_components", fake_create)
    monkeypatch.setattr("trove.services.jobs.runner.SchedulerRunner", _FakeRunner)

    await schedule_cmds.main_schedule(["--once"])

    assert captured["sm"] is session_manager
    assert captured["decision"] is components["decision"]
    assert captured["verdicts"] is components["verdicts"]
    assert captured["actions"] is components["actions"]
    assert captured["subscriptions"] is components["subscriptions"]
    assert captured["scans"] is components["scans"]


@pytest.mark.asyncio
async def test_schedule_reconciles_before_tick_and_disposes_its_store(
        tmp_path, monkeypatch):
    """启动收敛发生在第一次 tick 之前;cwd-rooted store 在退出时释放。

    顺序是承重的:先 tick 会把本进程刚写下的在飞行行也收成 error。store 释放
    是 aiosqlite 纪律 —— 它的 worker 是非 daemon 线程,不 dispose 会挂住进程。
    """
    from trove.cli import schedule_cmds

    events: list[str] = []

    class _FakeStore:
        async def dispose(self):
            events.append("dispose")

    class _FakeJobs:
        store = _FakeStore()

        async def reconcile_interrupted_runs(self):
            events.append("reconcile")
            return 0

    class _Registry:
        async def close_all(self):
            return None

    class _FakeRunner:
        def __init__(self, sm, jobs, **kw):
            events.append("construct")

        async def tick(self):
            events.append("tick")
            return []

    class _FakeCheckpointer:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def fake_load_config(args):
        from trove.core.config import AgentConfig

        return AgentConfig()

    async def fake_create(args, config, checkpointer):
        return {
            "session_manager": object(),
            "connector_registry": _Registry(),
            "decision": object(),
            "verdicts": object(),
            "actions": object(),
            "subscriptions": object(),
            "scans": object(),
        }

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(schedule_cmds, "_load_config_with", fake_load_config)
    monkeypatch.setattr("trove.main.build_checkpointer",
                        lambda home: _FakeCheckpointer())
    monkeypatch.setattr("trove.main.create_app_components", fake_create)
    monkeypatch.setattr("trove.services.jobs.runner.SchedulerRunner", _FakeRunner)
    monkeypatch.setattr(schedule_cmds, "_jobs_service", lambda: _FakeJobs())

    await schedule_cmds.main_schedule(["--once"])

    assert events == ["reconcile", "construct", "tick", "dispose"]


@pytest.mark.asyncio
async def test_daemon_exit_drains_records_and_disposes_store(
        tmp_path, monkeypatch):
    """收尾走 ``_dispose_components``:终态记录落盘 + store 释放。

    旧收尾只 ``connector_registry.close_all()``:挂在场上的终态 flush 被丢,
    aiosqlite worker 线程还会挂住进程退出。
    """
    from types import SimpleNamespace

    from trove.cli import schedule_cmds
    from trove.storage.run_record_store import RunRecordStore
    from trove.tracing.run_record import (
        configure_run_records,
        get_run_recorder,
        reset_run_records,
    )

    store = RunRecordStore(tmp_path)
    configure_run_records(store, enabled=True)
    rec = get_run_recorder()
    assert rec is not None
    components = {
        "session_manager": object(),
        "run_records": store,
        "decision": object(),
        "verdicts": object(),
        "actions": object(),
        "subscriptions": object(),
        "scans": object(),
    }

    class _FakeRunner:
        def __init__(self, sm, jobs, **kw):
            pass

        async def tick(self):
            rec.capture_run_start("r1", question="q?")
            rec.capture_finish("r1", final=SimpleNamespace(
                sql="SELECT 1", verdict="OK", row_count=1, error=""))
            return []

    class _FakeCheckpointer:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def fake_load_config(args):
        from trove.core.config import AgentConfig

        return AgentConfig()

    async def fake_create(args, config, checkpointer):
        return components

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(schedule_cmds, "_load_config_with", fake_load_config)
    monkeypatch.setattr("trove.main.build_checkpointer",
                        lambda home: _FakeCheckpointer())
    monkeypatch.setattr("trove.main.create_app_components", fake_create)
    monkeypatch.setattr("trove.services.jobs.runner.SchedulerRunner", _FakeRunner)
    try:
        await schedule_cmds.main_schedule(["--once"])
    finally:
        reset_run_records()

    # 先 drain 再 dispose:终态记录已落盘,连接已释放(读会按需求重连)。
    assert store._backend._conn is None, "daemon 退出后 store 必须已释放"
    record = await store.get("r1")
    assert record is not None
    assert record["header"]["status"] == "done"
    await store.dispose()


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