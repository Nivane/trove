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

    Records adelete_thread calls; alist/prune are no-ops so run_all()
    (Task 4) can reuse this fake.
    """

    class FakeCheckpointer:
        def __init__(self):
            self.deleted: list[str] = []

        async def adelete_thread(self, thread_id: str) -> None:
            self.deleted.append(thread_id)

        async def alist(self, config=None):  # async generator: no threads
            if False:
                yield  # pragma: no cover

        async def aprune(self, thread_id: str, depth: int) -> None:
            pass

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
