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

    async def test_login_response_has_expires_at(self, anon_client, auth_service):
        resp = await anon_client.post(
            "/v1/auth/login", json={"username": "admin", "password": "adminpw"}
        )
        assert resp.status_code == 200
        expires_at = resp.json()["expires_at"]
        assert expires_at is not None
        from datetime import datetime, timezone

        remaining = (
            datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)
        ).total_seconds()
        assert 0 < remaining <= auth_service.token_ttl_hours * 3600

    async def test_login_rate_limited_429_with_retry_after(self, anon_client):
        for _ in range(5):  # 5 failures recorded → 6th attempt is locked
            resp = await anon_client.post(
                "/v1/auth/login", json={"username": "admin", "password": "wrong"}
            )
            assert resp.status_code == 401
        resp = await anon_client.post(
            "/v1/auth/login", json={"username": "admin", "password": "adminpw"}
        )
        assert resp.status_code == 429
        assert resp.headers.get("retry-after") is not None
        # Even the correct password stays locked while the window is hot
        resp = await anon_client.post(
            "/v1/auth/login", json={"username": "admin", "password": "adminpw"}
        )
        assert resp.status_code == 429

    async def test_login_attempts_do_not_block_other_user(self, anon_client):
        for _ in range(5):
            await anon_client.post(
                "/v1/auth/login", json={"username": "bob", "password": "wrong"}
            )
        resp = await anon_client.post(
            "/v1/auth/login", json={"username": "admin", "password": "adminpw"}
        )
        assert resp.status_code == 200

    async def test_login_success_resets_lockout(self, anon_client, auth_service):
        for _ in range(5):
            await anon_client.post(
                "/v1/auth/login", json={"username": "admin", "password": "wrong"}
            )
        assert (
            await anon_client.post(
                "/v1/auth/login", json={"username": "admin", "password": "adminpw"}
            )
        ).status_code == 429
        # A successful attempt (e.g. via another surface) clears the counter
        await auth_service.record_login_attempt("admin", "1.2.3.4", success=True)
        resp = await anon_client.post(
            "/v1/auth/login", json={"username": "admin", "password": "adminpw"}
        )
        assert resp.status_code == 200


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


class TestTokenScopes:
    """受限 token:scopes 为最小权限 allowlist,路由级裁决。"""

    async def _mint(self, auth_service, username: str, scopes: list[str]):
        user = await auth_service.authenticate(username, "bobpw" if username == "bob" else "adminpw")
        raw, _ = await auth_service.create_token(
            user["id"], label=f"scoped-{username}", scopes=scopes,
        )
        return raw

    async def test_query_scope_can_chat_not_admin(self, anon_client, auth_service):
        """admin 账号签发 query 专用 token:可查会话面,不可管(403)。"""
        token = await self._mint(auth_service, "admin", ["query"])
        headers = {"Authorization": f"Bearer {token}"}

        ok = await anon_client.get("/v1/sessions", headers=headers)
        assert ok.status_code == 200  # 查询面放行

        denied = await anon_client.get("/v1/admin/audit", headers=headers)
        assert denied.status_code == 403  # 管理面被 token scope 拦下

    async def test_admin_scope_can_admin_not_query(self, anon_client, auth_service):
        """scopes=['admin']:管理端点放行,查询面 403。"""
        token = await self._mint(auth_service, "admin", ["admin"])
        headers = {"Authorization": f"Bearer {token}"}

        ok = await anon_client.get("/v1/admin/audit", headers=headers)
        assert ok.status_code == 200

        denied = await anon_client.get("/v1/sessions", headers=headers)
        assert denied.status_code == 403

    async def test_unrestricted_token_unaffected(self, anon_client, auth_service, admin_token):
        """未声明 scopes 的存量 token 行为不变:两面都放行。"""
        headers = {"Authorization": f"Bearer {admin_token}"}
        assert (await anon_client.get("/v1/sessions", headers=headers)).status_code == 200
        assert (await anon_client.get("/v1/admin/audit", headers=headers)).status_code == 200

    async def test_query_scope_token_for_non_admin_user(self, anon_client, auth_service):
        """普通用户 + query scope:查询面正常,管理面本就 403(角色 + scope 双栅栏)。"""
        token = await self._mint(auth_service, "bob", ["query"])
        headers = {"Authorization": f"Bearer {token}"}
        assert (await anon_client.get("/v1/sessions", headers=headers)).status_code == 200
        assert (await anon_client.get("/v1/admin/audit", headers=headers)).status_code == 403
