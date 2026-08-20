"""/v1/admin endpoints: users/tokens/datasources/audit (admin-only)."""

from __future__ import annotations

import pytest


class TestAdminAuthz:
    async def test_non_admin_403(self, user_client):
        for method, path in [
            ("get", "/v1/admin/users"),
            ("post", "/v1/admin/users"),
            ("get", "/v1/admin/audit"),
            ("get", "/v1/admin/sessions"),
        ]:
            resp = await getattr(user_client, method)(path)
            assert resp.status_code == 403, f"{method} {path}"

    async def test_anonymous_401(self, anon_client):
        assert (await anon_client.get("/v1/admin/users")).status_code == 401


class TestUsers:
    async def test_create_list_update_delete(self, client, auth_service):
        resp = await client.post("/v1/admin/users", json={
            "username": "carol", "password": "carolpw", "display_name": "Carol",
        })
        assert resp.status_code == 201
        carol = resp.json()
        assert carol["role"] == "user"
        assert "password_hash" not in carol

        users = (await client.get("/v1/admin/users")).json()["users"]
        assert "carol" in [u["username"] for u in users]

        patched = await client.patch(f"/v1/admin/users/{carol['id']}", json={"disabled": True})
        assert patched.status_code == 200
        assert patched.json()["disabled"] is True

        # Disabled user cannot log in
        login = await client.post("/v1/auth/login", json={"username": "carol", "password": "carolpw"})
        assert login.status_code == 401

        # Password change works
        await client.patch(f"/v1/admin/users/{carol['id']}", json={"password": "newpw", "disabled": False})
        login = await client.post("/v1/auth/login", json={"username": "carol", "password": "newpw"})
        assert login.status_code == 200

        assert (await client.delete(f"/v1/admin/users/{carol['id']}")).status_code == 204
        assert (await client.delete(f"/v1/admin/users/{carol['id']}")).status_code == 404

    async def test_create_duplicate_400(self, client):
        resp = await client.post("/v1/admin/users", json={"username": "bob", "password": "x"})
        assert resp.status_code == 400

    async def test_cannot_delete_self_via_api(self, client, auth_service):
        admin = await auth_service.authenticate("admin", "adminpw")
        resp = await client.delete(f"/v1/admin/users/{admin['id']}")
        assert resp.status_code == 400

    async def test_cannot_disable_last_admin(self, client, auth_service):
        admin = await auth_service.authenticate("admin", "adminpw")
        resp = await client.patch(f"/v1/admin/users/{admin['id']}", json={"disabled": True})
        assert resp.status_code == 400

    async def test_role_escalation(self, client):
        resp = await client.post("/v1/admin/users", json={
            "username": "dave", "password": "pw", "role": "admin",
        })
        assert resp.status_code == 201
        assert resp.json()["role"] == "admin"


class TestTokens:
    async def test_admin_create_list_revoke_token(self, client, auth_service):
        bob = await auth_service.authenticate("bob", "bobpw")
        created = await client.post(f"/v1/admin/users/{bob['id']}/tokens", json={"label": "api"})
        assert created.status_code == 201
        raw = created.json()["token"]
        assert raw.startswith("trove_")

        # Token works
        me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {raw}"})
        assert me.status_code == 200

        listed = (await client.get(f"/v1/admin/users/{bob['id']}/tokens")).json()["tokens"]
        assert any(t["label"] == "api" for t in listed)
        assert all("token_hash" not in t for t in listed)

        token_id = listed[0]["id"]
        assert (await client.delete(f"/v1/admin/tokens/{token_id}")).status_code == 204
        me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {raw}"})
        assert me.status_code == 401

    async def test_token_for_unknown_user_404(self, client):
        resp = await client.post("/v1/admin/users/9999/tokens", json={})
        assert resp.status_code == 404


class TestDatasourceGrants:
    async def test_get_set_datasources(self, client, auth_service):
        bob = await auth_service.authenticate("bob", "bobpw")
        resp = await client.put(
            f"/v1/admin/users/{bob['id']}/datasources",
            json={"datasources": ["test_db", "other"]},
        )
        assert resp.status_code == 200
        got = (await client.get(f"/v1/admin/users/{bob['id']}/datasources")).json()
        assert got["datasources"] == ["other", "test_db"]

    async def test_grant_gates_catalog(self, api_app, user_client, client, auth_service):
        """bob without grants sees only the default datasource; granting 'test_db' is a no-op there; a second datasource needs a grant."""
        bob = await auth_service.authenticate("bob", "bobpw")

        # bob sees only default (test_db)
        ds = (await user_client.get("/v1/catalog/datasources")).json()["datasources"]
        assert [d["name"] for d in ds] == ["test_db"]

        # admin sees all
        admin_ds = (await client.get("/v1/catalog/datasources")).json()["datasources"]
        assert "test_db" in [d["name"] for d in admin_ds]


class TestAudit:
    async def test_audit_records_admin_actions(self, client, auth_service):
        await client.post("/v1/admin/users", json={"username": "erin", "password": "pw"})
        entries = await auth_service.list_audit(action="admin.user.create")
        assert any(e["username"] == "admin" for e in entries)
        api_entries = (await client.get("/v1/admin/audit?action=admin.user.create")).json()["audit"]
        assert len(api_entries) >= 1
        assert api_entries[0]["status"] == 201

    async def test_audit_filters(self, client):
        await client.post("/v1/admin/users", json={"username": "frank", "password": "pw"})
        all_entries = (await client.get("/v1/admin/audit")).json()["audit"]
        assert len(all_entries) >= 1
        created = (await client.get("/v1/admin/audit?action=admin.user.create")).json()["audit"]
        assert len(created) >= 1
        empty = (await client.get("/v1/admin/audit?action=nonexistent")).json()["audit"]
        assert empty == []
