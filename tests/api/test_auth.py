"""/v1/auth endpoints: login, me, logout, token enforcement."""

from __future__ import annotations


class TestLogin:
    async def test_login_success(self, anon_client):
        resp = await anon_client.post(
            "/v1/auth/login", json={"username": "admin", "password": "adminpw"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["token"].startswith("trove_")
        assert body["user"]["username"] == "admin"
        assert body["user"]["role"] == "admin"

    async def test_login_bad_password_401(self, anon_client):
        resp = await anon_client.post(
            "/v1/auth/login", json={"username": "admin", "password": "wrong"}
        )
        assert resp.status_code == 401

    async def test_login_unknown_user_401(self, anon_client):
        resp = await anon_client.post(
            "/v1/auth/login", json={"username": "ghost", "password": "x"}
        )
        assert resp.status_code == 401

    async def test_login_disabled_user_401(self, anon_client, auth_service):
        bob = await auth_service.authenticate("bob", "bobpw")
        await auth_service.update_user(bob["id"], disabled=True)
        resp = await anon_client.post(
            "/v1/auth/login", json={"username": "bob", "password": "bobpw"}
        )
        assert resp.status_code == 401

    async def test_login_audits_both_paths(self, anon_client, auth_service):
        await anon_client.post("/v1/auth/login", json={"username": "admin", "password": "adminpw"})
        await anon_client.post("/v1/auth/login", json={"username": "admin", "password": "nope"})
        logins = await auth_service.list_audit(action="auth.login")
        assert len(logins) == 2
        assert sorted(e["status"] for e in logins) == [200, 401]


class TestMe:
    async def test_me_with_token(self, client):
        resp = await client.get("/v1/auth/me")
        assert resp.status_code == 200
        assert resp.json()["user"]["username"] == "admin"

    async def test_me_requires_auth(self, anon_client):
        assert (await anon_client.get("/v1/auth/me")).status_code == 401


class TestLogout:
    async def test_logout_revokes_token(self, anon_client, auth_service):
        login = await anon_client.post(
            "/v1/auth/login", json={"username": "admin", "password": "adminpw"}
        )
        token = login.json()["token"]
        resp = await anon_client.post(
            "/v1/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        # Same token is dead now
        me = await anon_client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert me.status_code == 401


class TestTokenEnforcement:
    async def test_missing_token_401(self, anon_client):
        resp = await anon_client.get("/v1/kb/status")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"

    async def test_garbage_bearer_401(self, anon_client):
        resp = await anon_client.get(
            "/v1/kb/status", headers={"Authorization": "Bearer garbage"}
        )
        assert resp.status_code == 401

    async def test_non_bearer_header_401(self, anon_client):
        resp = await anon_client.get(
            "/v1/kb/status", headers={"Authorization": "Basic abc"}
        )
        assert resp.status_code == 401

    async def test_revoked_token_401(self, anon_client, auth_service, user_token):
        # user_token belongs to bob; revoke it via the service directly
        tokens = await auth_service.list_tokens(
            (await auth_service.authenticate("bob", "bobpw"))["id"]
        )
        await auth_service.revoke_token(tokens[0]["id"])
        resp = await anon_client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {user_token}"}
        )
        assert resp.status_code == 401
