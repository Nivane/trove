"""User facts store/service tests: CRUD, timestamps, scoping, ownership, search."""

from __future__ import annotations

import pytest

from trove.services.user_facts.service import UserFactsService
from trove.services.user_facts.store import UserFactsStore


@pytest.fixture
async def svc(tmp_path):
    return UserFactsService(tmp_path / "user_facts.db")


class TestUserFactsStore:
    async def test_add_returns_row_with_timestamps(self, tmp_path):
        store = UserFactsStore(tmp_path / "facts.db")
        row = await store.add("alice", "demo", "营收 = 净收入")
        assert row["id"] >= 1
        assert row["user_id"] == "alice"
        assert row["datasource"] == "demo"
        assert row["fact"] == "营收 = 净收入"
        assert row["created_at"] and row["updated_at"]

    async def test_update_keeps_created_at_bumps_updated_at(self, tmp_path):
        store = UserFactsStore(tmp_path / "facts.db")
        row = await store.add("alice", "demo", "old")
        updated = await store.update("alice", row["id"], fact="new")
        assert updated["fact"] == "new"
        assert updated["created_at"] == row["created_at"]
        assert updated["updated_at"] != row["updated_at"]

    async def test_update_cross_user_returns_none(self, tmp_path):
        store = UserFactsStore(tmp_path / "facts.db")
        row = await store.add("alice", "demo", "fact")
        assert await store.update("bob", row["id"], fact="x") is None
        assert await store.get("alice", row["id"]) is not None

    async def test_delete_scoped_to_owner(self, tmp_path):
        store = UserFactsStore(tmp_path / "facts.db")
        row = await store.add("alice", "demo", "fact")
        assert not await store.delete("bob", row["id"])
        assert await store.delete("alice", row["id"])
        assert await store.get("alice", row["id"]) is None

    async def test_list_filters_by_datasource(self, tmp_path):
        store = UserFactsStore(tmp_path / "facts.db")
        await store.add("alice", "demo", "f1")
        await store.add("alice", "other", "f2")
        rows = await store.list("alice", "demo")
        assert [r["fact"] for r in rows] == ["f1"]
        assert [r["fact"] for r in await store.list("alice")] == ["f2", "f1"]

    async def test_owner_isolation(self, tmp_path):
        store = UserFactsStore(tmp_path / "facts.db")
        a = await store.add("alice", "demo", "f1")
        await store.add("bob", "demo", "f2")
        assert await store.get("bob", a["id"]) is None
        assert [r["fact"] for r in await store.list("alice")] == ["f1"]

    async def test_delete_any_crosses_owner(self, tmp_path):
        store = UserFactsStore(tmp_path / "facts.db")
        a = await store.add("alice", "demo", "f1")
        assert await store.get_any(a["id"]) is not None
        assert await store.delete_any(a["id"])
        assert await store.get_any(a["id"]) is None


class TestUserFactsService:
    async def test_add_rejects_empty(self, svc):
        with pytest.raises(ValueError):
            await svc.add("alice", "demo", "   ")

    async def test_search_ranks_relevant_first(self, svc):
        await svc.add("alice", "demo", "营收口径 = 净收入")
        await svc.add("alice", "demo", "看日均用 30 日均值")
        hits = await svc.search("alice", "demo", "营收怎么算", limit=1)
        assert len(hits) == 1
        assert "净收入" in hits[0]["fact"]

    async def test_search_limited_and_scoped(self, svc):
        await svc.add("alice", "demo", "营收 = 净收入")
        await svc.add("alice", "demo", "营收 = 含税")
        await svc.add("alice", "demo", "营收 = 不含税")
        hits = await svc.search("alice", "demo", "营收", limit=2)
        assert len(hits) == 2
        assert all("营收" in f["fact"] for f in hits)

    async def test_search_user_and_datasource_isolation(self, svc):
        await svc.add("alice", "demo", "营收 = 净收入")
        await svc.add("bob", "demo", "营收 = 毛收入")
        assert [f["fact"] for f in await svc.search("alice", "demo", "营收")] == ["营收 = 净收入"]
        assert [f["fact"] for f in await svc.search("bob", "demo", "营收")] == ["营收 = 毛收入"]
        assert await svc.search("alice", "other", "营收") == []

    async def test_admin_list_all_and_delete_any(self, svc):
        a = await svc.add("alice", "demo", "f1")
        await svc.add("bob", "demo", "f2")
        await svc.add("bob", "other", "f3")
        assert len(await svc.list_all()) == 3
        assert len(await svc.list_all(datasource="demo")) == 2
        assert len(await svc.list_all(user_id="bob")) == 2
        assert await svc.delete_any(a["id"])
        assert await svc.get("alice", a["id"]) is None


class TestWritePolicy:
    """写入策略:存什么不存什么(对应记忆深度版"写入什么")。"""

    async def test_rejects_too_short(self, svc):
        with pytest.raises(ValueError):
            await svc.add("alice", "demo", "x")

    async def test_rejects_too_long(self, svc):
        with pytest.raises(ValueError):
            await svc.add("alice", "demo", "长" * 301)

    async def test_rejects_no_content(self, svc):
        with pytest.raises(ValueError):
            await svc.add("alice", "demo", "!!!...###")

    async def test_normalizes_whitespace(self, svc):
        row = await svc.add("alice", "demo", "  营收 =   净收入  ")
        assert row["fact"] == "营收 = 净收入"

    async def test_update_validates_content(self, svc):
        row = await svc.add("alice", "demo", "营收 = 净收入")
        with pytest.raises(ValueError):
            await svc.update("alice", row["id"], fact="x")
        assert (await svc.get("alice", row["id"]))["fact"] == "营收 = 净收入"

    async def test_update_unknown_returns_none_before_validation(self, svc):
        """归属检查先于内容校验:不存在的事实不因坏内容报错。"""
        assert await svc.update("alice", 9999, fact="x") is None


class TestConflictResolution:
    """冲突消解:等值事实幂等刷新,不重复堆积(防记忆污染)。"""

    async def test_duplicate_add_refreshes_same_row(self, svc):
        r1 = await svc.add("alice", "demo", "营收 = 净收入")
        r2 = await svc.add("alice", "demo", " 营收 =  净收入 ")  # 规范化等值
        assert r1["id"] == r2["id"]
        assert len(await svc.list("alice", "demo")) == 1

    async def test_duplicate_add_bumps_updated_at(self, svc):
        r1 = await svc.add("alice", "demo", "营收 = 净收入")
        r2 = await svc.add("alice", "demo", "营收 = 净收入")
        assert r2["created_at"] == r1["created_at"]
        assert r2["updated_at"] >= r1["updated_at"]

    async def test_different_text_stays_separate(self, svc):
        await svc.add("alice", "demo", "营收 = 净收入")
        await svc.add("alice", "demo", "营收 = 毛收入")
        assert len(await svc.list("alice", "demo")) == 2


class TestMemoryLifecycle:
    """遗忘/压缩:时间衰减排序 + 超期不注入 + 物理清理(对应"什么时候遗忘")。"""

    async def _backdate(self, svc, fact_id, days):
        """把 updated_at 回拨 days 天(模拟久远事实)。"""
        from datetime import datetime, timedelta, timezone

        ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        await svc.store.update("alice", fact_id, fact="__touch__")
        import aiosqlite

        async with aiosqlite.connect(svc.store.db_path) as db:
            await db.execute(
                "UPDATE user_facts SET updated_at = ? WHERE id = ?", (ts, fact_id)
            )
            await db.commit()

    async def test_expired_fact_excluded_from_search(self, svc):
        a = await svc.add("alice", "demo", "营收 = 净收入")
        b = await svc.add("alice", "demo", "看日均用 30 日均值")
        await self._backdate(svc, a["id"], days=365)
        await self._backdate(svc, b["id"], days=365)  # 全部超期
        assert await svc.search("alice", "demo", "日均") == []

    async def test_expired_still_visible_in_raw_list(self, svc):
        """遗忘发生在注入边界;原始列表(CRUD 视图)仍可见。"""
        row = await svc.add("alice", "demo", "营收 = 净收入")
        await self._backdate(svc, row["id"], days=365)
        assert await svc.list("alice", "demo")  # 仍可查
        assert await svc.search("alice", "demo", "营收") == []  # 但不注入

    async def test_update_revives_fact(self, svc):
        row = await svc.add("alice", "demo", "营收 = 净收入")
        await self._backdate(svc, row["id"], days=365)
        assert await svc.search("alice", "demo", "营收") == []
        await svc.update("alice", row["id"], fact="营收 = 净收入(本年)")  # 触碰 → 复活
        assert await svc.search("alice", "demo", "营收")

    async def test_recency_ranks_fresh_first_on_tie(self, svc):
        a = await svc.add("alice", "demo", "营收 = 净收入")
        b = await svc.add("alice", "demo", "营收 = 含税口径")
        await self._backdate(svc, a["id"], days=150)  # 同相关度,更旧 → 排后
        hits = await svc.search("alice", "demo", "营收")
        assert hits[0]["fact"] == "营收 = 含税口径"

    async def test_purge_expired_removes_physically(self, svc):
        old = await svc.add("alice", "demo", "营收 = 净收入")
        fresh = await svc.add("alice", "demo", "看日均用 30 日均值")
        await self._backdate(svc, old["id"], days=365)
        assert await svc.purge_expired(days=180) == 1
        remaining = await svc.list("alice", "demo")
        assert [r["id"] for r in remaining] == [fresh["id"]]
