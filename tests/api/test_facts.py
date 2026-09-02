"""User facts API tests: self-service CRUD, ownership isolation, admin cross-user."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from trove.services.user_facts.service import UserFactsService


@pytest.fixture
async def facts_app(api_app, tmp_path):
    """api_app with a real user_facts service attached (default ds = test_db)."""
    service = UserFactsService(tmp_path / "user_facts.db")
    api_app.state.user_facts = service
    yield api_app
    await service.dispose()


async def _client(app, token):
    transport = ASGITransport(app=app)
    return AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


class TestFactsCRUD:
    async def test_requires_auth(self, facts_app, anon_client):
        assert (await anon_client.get("/v1/facts")).status_code == 401

    async def test_add_list_update_delete(self, facts_app, user_token):
        async with await _client(facts_app, user_token) as c:
            r = await c.post("/v1/facts", json={"datasource": "test_db", "fact": "营收 = 净收入"})
            assert r.status_code == 201
            fid = r.json()["fact"]["id"]

            r = await c.get("/v1/facts")
            assert r.status_code == 200
            facts = r.json()["facts"]
            assert len(facts) == 1
            assert facts[0]["fact"] == "营收 = 净收入"
            assert facts[0]["datasource"] == "test_db"

            r = await c.patch(f"/v1/facts/{fid}", json={"fact": "营收 = 毛收入"})
            assert r.status_code == 200
            assert r.json()["fact"]["fact"] == "营收 = 毛收入"

            r = await c.delete(f"/v1/facts/{fid}")
            assert r.status_code == 204
            assert (await c.get("/v1/facts")).json()["facts"] == []

    async def test_patch_missing_returns_404(self, facts_app, user_token):
        async with await _client(facts_app, user_token) as c:
            r = await c.patch("/v1/facts/9999", json={"fact": "x"})
            assert r.status_code == 404

    async def test_delete_missing_returns_404(self, facts_app, user_token):
        async with await _client(facts_app, user_token) as c:
            assert (await c.delete("/v1/facts/9999")).status_code == 404

    async def test_search_preview(self, facts_app, user_token):
        async with await _client(facts_app, user_token) as c:
            await c.post("/v1/facts", json={"datasource": "test_db", "fact": "营收 = 净收入"})
            r = await c.get("/v1/facts", params={"q": "营收", "datasource": "test_db"})
            assert r.status_code == 200
            assert len(r.json()["facts"]) == 1
            assert r.json()["facts"][0]["fact"] == "营收 = 净收入"


class TestFactsOwnership:
    async def test_self_route_never_sees_others(self, facts_app, user_token, admin_token):
        async with await _client(facts_app, user_token) as c:
            r = await c.post("/v1/facts", json={"datasource": "test_db", "fact": "bob fact"})
            fid = r.json()["fact"]["id"]
        async with await _client(facts_app, admin_token) as c:
            # admin's own fact list does not include bob's
            assert (await c.get("/v1/facts")).json()["facts"] == []
            # admin cannot mutate bob's fact through the self route
            assert (await c.delete(f"/v1/facts/{fid}")).status_code == 404
            assert (await c.patch(f"/v1/facts/{fid}", json={"fact": "x"})).status_code == 404

    async def test_admin_cross_user_view_and_delete(self, facts_app, user_token, admin_token):
        async with await _client(facts_app, user_token) as c:
            r = await c.post("/v1/facts", json={"datasource": "test_db", "fact": "bob fact"})
            fid = r.json()["fact"]["id"]
        async with await _client(facts_app, admin_token) as c:
            r = await c.get("/v1/admin/facts")
            assert r.status_code == 200
            assert len(r.json()["facts"]) == 1
            assert r.json()["facts"][0]["user_id"] == "2"  # bob (admin id=1)
            assert (await c.delete(f"/v1/admin/facts/{fid}")).status_code == 204
        async with await _client(facts_app, user_token) as c:
            assert (await c.get("/v1/facts")).json()["facts"] == []

    async def test_admin_facts_requires_admin(self, facts_app, user_token):
        async with await _client(facts_app, user_token) as c:
            assert (await c.get("/v1/admin/facts")).status_code == 403
            assert (await c.delete("/v1/admin/facts/1")).status_code == 403
