"""MaintenanceService 会话配额清理测试(真实 SQLite,临时 home)。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trove.core.config import RetentionConfig
from trove.core.types import Message
from trove.storage.session_store import SessionStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _retention(max_sessions: int = 100, grace_min: int = 10) -> RetentionConfig:
    return RetentionConfig(
        max_sessions_per_user=max_sessions,
        active_grace_min=grace_min,
        max_checkpoints_per_thread=50,
        sweep_interval_hours=24,
    )


def _fake_checkpointer():
    """Duck-typed checkpointer for quota-sweep tests.

    Records adelete_thread calls; alist yields no threads, so run_all()
    (Task 4: orphans/prune) degrades to zero work on this fake.
    """

    class FakeCheckpointer:
        def __init__(self):
            self.deleted: list[str] = []

        async def adelete_thread(self, thread_id: str) -> None:
            self.deleted.append(thread_id)

        async def alist(self, config=None):  # async generator: no threads
            if False:
                yield  # pragma: no cover

    return FakeCheckpointer()


async def _seed(store: SessionStore, project: str, user: str, *, updated_delta_min: int = 0) -> str:
    """Create a session whose meta.updated_at is now - updated_delta_min."""
    session = await store.create_session(project, user_id=user)
    session.messages = [
        Message(role="user", content="q", timestamp=_utcnow(), metadata={})
    ]
    await store.save_session(session)
    if updated_delta_min:
        # Rewrite updated_at in meta to simulate aging
        import aiosqlite
        db = store.session_db_path(project, session.session_id)
        conn = await aiosqlite.connect(str(db))
        await conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('updated_at', ?)",
            ((_utcnow() - timedelta(minutes=updated_delta_min)).isoformat(),),
        )
        await conn.commit()
        await conn.close()
    return session.session_id


async def test_sweep_removes_oldest_beyond_quota(tmp_home):
    """5 用户 × 21 会话,配额 20:删最旧的 1 个,checkpoint 级联删除。"""
    from trove.services.maintenance import MaintenanceService

    store = SessionStore(home_dir=str(tmp_home))
    ckpt = _fake_checkpointer()
    ids = []
    for u in range(5):
        for i in range(21):
            ids.append(await _seed(store, "proj", f"user{u}", updated_delta_min=i + 1))
    svc = MaintenanceService(store, ckpt, _retention(max_sessions=20, grace_min=0))
    stats = await svc.sweep()
    assert stats.removed_sessions == 5
    assert stats.scanned == 105
    assert stats.errors == 0
    remaining = await store.list_all()
    assert len(remaining) == 100
    # 删除的是每用户最旧(updated_delta_min 最大 = 最旧)
    for u in range(5):
        group = [s for s in remaining if s["user_id"] == f"user{u}"]
        assert len(group) == 20
    # checkpoint 级联:被删 session 的 thread_id 都调了 adelete_thread
    deleted_expected = {f"user{u}" for u in range(5)}
    # 找到被删的 session_id:105 个 id 减剩余 100
    remaining_ids = {s["session_id"] for s in remaining}
    gone = set(ids) - remaining_ids
    assert len(gone) == 5
    assert set(ckpt.deleted) == gone


async def test_sweep_quota_zero_disables(tmp_home):
    """max_sessions_per_user=0 时不删任何会话。"""
    from trove.services.maintenance import MaintenanceService

    store = SessionStore(home_dir=str(tmp_home))
    ckpt = _fake_checkpointer()
    for i in range(3):
        await _seed(store, "proj", "alice", updated_delta_min=i + 1)
    svc = MaintenanceService(store, ckpt, _retention(max_sessions=0, grace_min=0))
    stats = await svc.sweep()
    assert stats.removed_sessions == 0
    assert len(await store.list_all()) == 3
    assert ckpt.deleted == []


async def test_sweep_active_grace_exempts_fresh(tmp_home):
    """候选者全在 grace 窗口内时被豁免(真实覆盖豁免分支);grace=0 对照组删除发生。"""
    from trove.services.maintenance import MaintenanceService

    store = SessionStore(home_dir=str(tmp_home))
    ckpt = _fake_checkpointer()
    # 3 个会话 1/2/3 分钟前(全部在 10 分钟窗口内);配额 2、grace 10
    for delta in (1, 2, 3):
        await _seed(store, "proj", "alice", updated_delta_min=delta)
    svc = MaintenanceService(store, ckpt, _retention(max_sessions=2, grace_min=10))
    stats = await svc.sweep()
    assert stats.removed_sessions == 0  # 候选者(3 分钟前)被豁免
    assert stats.skipped_active == 1
    assert len(await store.list_all()) == 3  # 文件全部还在
    # 对照组:grace=0 关闭豁免 → 最旧者被删
    svc0 = MaintenanceService(store, ckpt, _retention(max_sessions=2, grace_min=0))
    stats0 = await svc0.sweep()
    assert stats0.removed_sessions == 1
    assert stats0.skipped_active == 0
    assert len(await store.list_all()) == 2


async def test_sweep_idempotent(tmp_home):
    """重复 sweep 第二次不再删除。"""
    from trove.services.maintenance import MaintenanceService

    store = SessionStore(home_dir=str(tmp_home))
    ckpt = _fake_checkpointer()
    for i in range(3):
        await _seed(store, "proj", "alice", updated_delta_min=i + 1)
    svc = MaintenanceService(store, ckpt, _retention(max_sessions=2, grace_min=0))
    stats1 = await svc.sweep()
    stats2 = await svc.sweep()
    assert stats1.removed_sessions == 1
    assert stats2.removed_sessions == 0


async def test_sweep_naive_updated_at_falls_back_to_mtime(tmp_home):
    """meta.updated_at 为无时区偏移(naive)的脏数据时不崩溃,回退文件 mtime 判活跃。"""
    from trove.services.maintenance import MaintenanceService

    store = SessionStore(home_dir=str(tmp_home))
    ckpt = _fake_checkpointer()
    old_id = await _seed(store, "proj", "alice", updated_delta_min=60)
    await _seed(store, "proj", "alice", updated_delta_min=30)
    # 把最旧会话的 updated_at 改写为 naive 时间戳(无时区偏移,脏数据)
    import aiosqlite
    db = store.session_db_path("proj", old_id)
    conn = await aiosqlite.connect(str(db))
    await conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('updated_at', ?)",
        ((_utcnow() - timedelta(minutes=120)).replace(tzinfo=None).isoformat(),),
    )
    await conn.commit()
    await conn.close()
    svc = MaintenanceService(store, ckpt, _retention(max_sessions=1, grace_min=10))
    stats = await svc.sweep()
    assert stats.errors == 0  # 不崩溃
    assert stats.skipped_active == 1  # 回退 mtime(新文件)→ 活跃 → 豁免
    assert stats.removed_sessions == 0
    assert len(await store.list_all()) == 2  # 文件全部还在


# ---------------------------------------------------------------------------
# Task 4: purge_orphan_checkpoints / prune_thread_depth / run_all
# 校准说明(锁定 langgraph 1.2.11 / checkpoint-sqlite 3.1.1):
#   - aput(config, checkpoint, metadata, new_versions) 四参全必填;config 需
#     checkpoint_ns,checkpoint dict 需 id 键。
#   - alist 产出 CheckpointTuple 对象(非 (thread_id, checkpoint) 元组)。
#   - aprune 为 NotImplementedError stub → 深度修剪走 alist+adelete_thread+重写。
# ---------------------------------------------------------------------------


def _ckpt_row(step: int) -> dict:
    """Minimal graph checkpoint dict the saver can round-trip (id is required)."""
    return {"id": f"c{step:02d}", "__metadata__": {"step": step}, "messages": []}


def _ckpt_meta(step: int) -> dict:
    return {"source": "test", "step": step, "writes": None, "score": None}


async def test_purge_orphan_checkpoints(tmp_home):
    """checkpoints.db 中无会话文件的 thread_id 被清掉,有主的保留。"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from trove.services.maintenance import MaintenanceService

    store = SessionStore(home_dir=str(tmp_home))
    ckpt_db = str(tmp_home / "checkpoints.db")
    async with AsyncSqliteSaver.from_conn_string(ckpt_db) as saver:
        # 有主线程 + 3 个无主线程(无主线程 01 塞 3 行,钉线程级计数:
        # 若按行计数会错成 5,线程级应为 3)
        sid = await _seed(store, "proj", "alice")
        orphans = ("00000000-0000-0000-0000-000000000001",
                   "00000000-0000-0000-0000-000000000002",
                   "00000000-0000-0000-0000-000000000003")
        for step in range(3):
            await saver.aput({"configurable": {"thread_id": orphans[0], "checkpoint_ns": ""}},
                             _ckpt_row(step), _ckpt_meta(step), {})
        for step, orphan in enumerate(orphans[1:], start=3):
            await saver.aput({"configurable": {"thread_id": orphan, "checkpoint_ns": ""}},
                             _ckpt_row(step), _ckpt_meta(step), {})
        await saver.aput({"configurable": {"thread_id": sid, "checkpoint_ns": ""}},
                         _ckpt_row(99), _ckpt_meta(99), {})
        svc = MaintenanceService(store, saver, _retention())
        removed = await svc.purge_orphan_checkpoints()
        assert removed == 3  # 3 个无主线程;01 有 3 行也只计 1
        # 有主线程仍在,checkpoints.db 总量只剩有主线程的 1 行
        tuples = [t async for t in saver.alist({"configurable": {"thread_id": sid}})]
        assert tuples  # 非空 = 还在
        assert len([t async for t in saver.alist(None)]) == 1


async def test_prune_thread_depth(tmp_home):
    """单线程 60 行 checkpoint,depth=50 → 精确保留最新 50 行且父链不打断。"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from trove.services.maintenance import MaintenanceService

    store = SessionStore(home_dir=str(tmp_home))
    ckpt_db = str(tmp_home / "checkpoints.db")
    async with AsyncSqliteSaver.from_conn_string(ckpt_db) as saver:
        tid = "aaaaaaaa-0000-0000-0000-000000000000"
        # 逐行按序插入并串起父链(c00 为 root,c01 的父 = c00,……)
        prev: str | None = None
        for i in range(60):
            cfg = {"configurable": {"thread_id": tid, "checkpoint_ns": ""}}
            if prev is not None:
                cfg["configurable"]["checkpoint_id"] = prev
            await saver.aput(cfg, _ckpt_row(i), _ckpt_meta(i), {})
            prev = f"c{i:02d}"
        svc = MaintenanceService(store, saver, _retention())
        pruned = await svc.prune_thread_depth(depth=50)
        assert pruned == 1
        remaining = [t async for t in saver.alist({"configurable": {"thread_id": tid}})]
        by_id = {t.checkpoint["id"]: t for t in remaining}
        # 精确钉"最新行保留":存活 id 集合 == 最新 50 行,最老 10 行被修剪
        # (自造 id 零填充、字典序与插入序一致)
        assert len(by_id) == 50
        assert set(by_id) == {f"c{i:02d}" for i in range(10, 60)}
        # 父指针保留:链内行的父 = 上一行 id;仅最老 kept 行(c10)的父
        # (c09)位于被删段内,悬空终止
        for i in range(11, 60):
            parent = by_id[f"c{i:02d}"].parent_config
            assert parent["configurable"]["checkpoint_id"] == f"c{i-1:02d}"
        assert by_id["c10"].parent_config["configurable"]["checkpoint_id"] == "c09"


async def test_run_all_returns_stats(tmp_home):
    """run_all 依次执行三阶段并返回完整统计。"""
    from trove.services.maintenance import MaintenanceService

    store = SessionStore(home_dir=str(tmp_home))
    ckpt = _fake_checkpointer()
    for i in range(3):
        await _seed(store, "proj", "alice", updated_delta_min=i + 1)
    svc = MaintenanceService(store, ckpt, _retention(max_sessions=2, grace_min=0))
    result = await svc.run_all()
    assert result["sweep"].removed_sessions == 1
    assert result["orphans"] == 0 and result["pruned"] == 0


async def test_run_all_sweep_failure_isolated(tmp_home):
    """sweep 抛错被 run_all 隔离,仍返回完整统计(任一步失败不影响其他步)。"""
    from trove.services.maintenance import MaintenanceService, SweepStats

    class _RaisingStore:
        async def list_all(self):
            raise RuntimeError("boom")

    svc = MaintenanceService(_RaisingStore(), _fake_checkpointer(), _retention())
    result = await svc.run_all()
    assert result["orphans"] == 0 and result["pruned"] == 0
    assert isinstance(result["sweep"], SweepStats)
    assert result["sweep"].errors == 1


async def test_prune_thread_depth_zero_disables(tmp_home):
    """显式 depth=0 关闭深度修剪(不再落回默认 depth)。"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from trove.services.maintenance import MaintenanceService

    store = SessionStore(home_dir=str(tmp_home))
    ckpt_db = str(tmp_home / "checkpoints.db")
    async with AsyncSqliteSaver.from_conn_string(ckpt_db) as saver:
        tid = "bbbbbbbb-0000-0000-0000-000000000000"
        for i in range(60):
            await saver.aput({"configurable": {"thread_id": tid, "checkpoint_ns": ""}},
                             _ckpt_row(i), _ckpt_meta(i), {})
        svc = MaintenanceService(store, saver, _retention())
        pruned = await svc.prune_thread_depth(depth=0)
        assert pruned == 0
        remaining = [t async for t in saver.alist({"configurable": {"thread_id": tid}})]
        assert len(remaining) == 60  # 未修剪


def test_parse_dt_malformed_returns_none():
    """_parse_dt 的 malformed/naive/非 str 输入分支。"""
    from trove.services.maintenance import _parse_dt

    assert _parse_dt("garbage") is None
    assert _parse_dt("2024-01-01T00:00:00") is None  # naive 无时区
    assert _parse_dt(None) is None  # TypeError 分支


async def test_prune_thread_depth_default_depth(tmp_home):
    """prune_thread_depth() 无参调用回落 retention.max_checkpoints_per_thread(50)。"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from trove.services.maintenance import MaintenanceService

    store = SessionStore(home_dir=str(tmp_home))
    ckpt_db = str(tmp_home / "checkpoints.db")
    async with AsyncSqliteSaver.from_conn_string(ckpt_db) as saver:
        tid = "cccccccc-0000-0000-0000-000000000000"
        for i in range(60):
            await saver.aput({"configurable": {"thread_id": tid, "checkpoint_ns": ""}},
                             _ckpt_row(i), _ckpt_meta(i), {})
        svc = MaintenanceService(store, saver, _retention())  # 默认 max_checkpoints_per_thread=50
        pruned = await svc.prune_thread_depth()
        assert pruned == 1
        remaining = [t async for t in saver.alist({"configurable": {"thread_id": tid}})]
        assert len(remaining) == 50


async def test_prune_thread_depth_active_grace_exempts(tmp_home):
    """最新 checkpoint 带 grace 窗口内时间戳 → 该线程被豁免;无时间戳照常修剪。"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from trove.services.maintenance import MaintenanceService

    store = SessionStore(home_dir=str(tmp_home))
    ckpt_db = str(tmp_home / "checkpoints.db")
    now = datetime.now(timezone.utc)
    async with AsyncSqliteSaver.from_conn_string(ckpt_db) as saver:
        active_tid = "dddddddd-0000-0000-0000-000000000000"
        idle_tid = "eeeeeeee-0000-0000-0000-000000000000"
        for i in range(60):
            meta = dict(_ckpt_meta(i), updated_at=now.isoformat())
            await saver.aput({"configurable": {"thread_id": active_tid, "checkpoint_ns": ""}},
                             _ckpt_row(i), meta, {})
            await saver.aput({"configurable": {"thread_id": idle_tid, "checkpoint_ns": ""}},
                             _ckpt_row(i), _ckpt_meta(i), {})
        svc = MaintenanceService(store, saver, _retention(grace_min=10))
        pruned = await svc.prune_thread_depth(depth=50)
        assert pruned == 1  # 只有无时间戳的 idle 线程被修剪
        active_remaining = [t async for t in saver.alist({"configurable": {"thread_id": active_tid}})]
        assert len(active_remaining) == 60  # 活跃线程被豁免


async def test_sweep_checkpoint_failure_keeps_file(tmp_home):
    """adelete_thread 抛错 → 半删保护:文件保留、removed 全 0、errors=1。"""
    from trove.services.maintenance import MaintenanceService

    class _FailingCheckpointer:
        async def adelete_thread(self, thread_id: str) -> None:
            raise RuntimeError("ckpt boom")

    store = SessionStore(home_dir=str(tmp_home))
    old_id = await _seed(store, "proj", "alice", updated_delta_min=60)
    await _seed(store, "proj", "alice", updated_delta_min=30)
    svc = MaintenanceService(store, _FailingCheckpointer(), _retention(max_sessions=1, grace_min=0))
    stats = await svc.sweep()
    assert stats.removed_sessions == 0
    assert stats.removed_checkpoints == 0
    assert stats.errors == 1
    assert store.session_db_path("proj", old_id).exists()  # 半删保护:文件还在
    assert len(await store.list_all()) == 2


async def test_sweep_file_delete_failure_continues(tmp_home, monkeypatch):
    """unlink 抛错 → 该会话记 errors=1,其余候选删除照常进行。"""
    from pathlib import Path

    from trove.services.maintenance import MaintenanceService

    store = SessionStore(home_dir=str(tmp_home))
    ckpt = _fake_checkpointer()
    for delta in (60, 30, 10):
        await _seed(store, "proj", "alice", updated_delta_min=delta)

    real_unlink = Path.unlink
    unlink_calls = {"n": 0}

    def _flaky_unlink(self, *args, **kwargs):
        unlink_calls["n"] += 1
        if unlink_calls["n"] == 1:
            raise PermissionError("simulated unlink failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr("pathlib.Path.unlink", _flaky_unlink)

    svc = MaintenanceService(store, ckpt, _retention(max_sessions=1, grace_min=0))
    stats = await svc.sweep()
    assert stats.errors == 1
    assert stats.removed_sessions == 1  # 第二个候选删除成功
    assert len(ckpt.deleted) == 2  # 两个候选都删了 checkpoint 链
    # 3 个会话 - 成功删除 1 - unlink 失败仍在 1 = 剩 2(unlink 失败不中断其余删除)
    assert len(await store.list_all()) == 2
