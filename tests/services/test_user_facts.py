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
