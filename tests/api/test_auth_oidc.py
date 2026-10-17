"""OIDC HTTP 端到端:登录跳转 / 一次性 state / 回调签发 opaque token。

IdP 由 ``tests.helpers.oidc.FakeIdp``(MockTransport)提供 —— 零真实网络。
最终断言的口径是**下游看到的东西**:签发的 token 能过 ``/auth/me``,也能过
MCP 走的 ``resolve_token``,且下游不知道用户是从哪扇门进来的。
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.helpers.oidc import CLIENT_ID, ISSUER, FakeIdp
from trove.core.config import OidcConfig
from trove.services.auth.oidc import OidcClient


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


async def _attach(app, idp: FakeIdp, **cfg) -> OidcClient:
    client = OidcClient(
        OidcConfig(enabled=True, issuer=ISSUER, client_id=CLIENT_ID, **cfg),
        http=httpx.AsyncClient(transport=idp.transport),
    )
    app.state.oidc = client
    return client


@pytest.fixture
async def oidc_app(api_app, idp):
    await _attach(api_app, idp)
    return api_app


async def _state_row(auth_service, state: str):
    """读一次性 state 行(nonce / verifier)给测试签 ID token 用。

    路由在回调里原子消费它,所以只能在回调之前读 —— 这正是这里的目的。
    """
    conn = await auth_service.store._conn()
    try:
        cursor = await conn.execute(
            "SELECT nonce, code_verifier, next_path FROM oidc_states WHERE state = ?",
            (state,),
        )
        return await cursor.fetchone()
    finally:
        # 只读探测也必须收口(存储后端每个操作以 commit/close 结束)
        await conn.close()


async def _start_login(client, idp: FakeIdp, auth_service, *, next_path: str = "/"):
    """走 /login,从 302 里取 state,并把服务端存的 nonce 填进 idp。"""
    resp = await client.get("/v1/auth/oidc/login", params={"next": next_path})
    assert resp.status_code == 302, resp.text
    params = parse_qs(urlsplit(resp.headers["location"]).query)
    state = params["state"][0]
    row = await _state_row(auth_service, state)
    assert row is not None
    idp.nonce = row[0]
    return state, params


def _fragment(location: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).fragment).items()}


class TestRouting:
    async def test_routes_404_when_oidc_is_not_configured(self, anon_client):
        for path in ("/v1/auth/oidc/status", "/v1/auth/oidc/login",
                     "/v1/auth/oidc/callback"):
            resp = await anon_client.get(path)
            assert resp.status_code == 404, path

    async def test_status_reports_enabled(self, oidc_app, anon_client):
        resp = await anon_client.get("/v1/auth/oidc/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["login_url"] == "/v1/auth/oidc/login"


class TestLoginRedirect:
    async def test_login_redirects_with_pkce_and_state(
            self, oidc_app, anon_client, idp, auth_service):
        resp = await anon_client.get(
            "/v1/auth/oidc/login", params={"next": "/console/decisions"})
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith(f"{ISSUER}/authorize?")
        params = parse_qs(urlsplit(location).query)
        assert params["response_type"] == ["code"]
        assert params["client_id"] == [CLIENT_ID]
        assert params["code_challenge_method"] == ["S256"]
        assert params["redirect_uri"] == ["http://test/v1/auth/oidc/callback"]
        assert params["state"][0] and params["nonce"][0]
        # state 行是一次性的,且 next 落库(回调时不再信 URL 参数)
        row = await _state_row(auth_service, params["state"][0])
        assert row is not None
        assert row[2] == "/console/decisions"

    async def test_open_redirect_next_is_neutralized(
            self, oidc_app, anon_client, idp, auth_service):
        state, _ = await _start_login(
            anon_client, idp, auth_service, next_path="//evil.example.com")
        row = await _state_row(auth_service, state)
        assert row[2] == "/"

    async def test_discovery_failure_lands_on_error_fragment(
            self, api_app, anon_client):
        bad = FakeIdp(issuer="https://other.example.com")
        await _attach(api_app, bad)
        resp = await anon_client.get("/v1/auth/oidc/login")
        assert resp.status_code == 303
        assert _fragment(resp.headers["location"])["error"] == "exchange_failed"


class TestCallback:
    async def test_full_flow_issues_a_working_token(
            self, oidc_app, anon_client, idp, auth_service):
        state, _ = await _start_login(anon_client, idp, auth_service)
        resp = await anon_client.get(
            "/v1/auth/oidc/callback", params={"code": "abc", "state": state})
        assert resp.status_code == 303, resp.text
        location = resp.headers["location"]
        frag = _fragment(location)
        assert frag["next"] == "/"
        token = frag["token"]
        assert token.startswith("trove_")
        # 下游(HTTP 与 MCP 同一扇门)只看到不透明 token
        me = await anon_client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["user"]["username"] == "alice"
        resolved = await auth_service.resolve_token(token)
        assert resolved is not None
        assert resolved["username"] == "alice"

    async def test_state_is_one_time(
            self, oidc_app, anon_client, idp, auth_service):
        state, _ = await _start_login(anon_client, idp, auth_service)
        first = await anon_client.get(
            "/v1/auth/oidc/callback", params={"code": "abc", "state": state})
        assert first.status_code == 303
        second = await anon_client.get(
            "/v1/auth/oidc/callback", params={"code": "abc", "state": state})
        assert second.status_code == 303
        assert _fragment(second.headers["location"])["error"] == "state_invalid"

    async def test_unknown_state_is_rejected(self, oidc_app, anon_client):
        resp = await anon_client.get(
            "/v1/auth/oidc/callback", params={"code": "abc", "state": "nope"})
        assert _fragment(resp.headers["location"])["error"] == "state_invalid"

    async def test_idp_error_is_exchange_failed(
            self, oidc_app, anon_client, idp, auth_service):
        state, _ = await _start_login(anon_client, idp, auth_service)
        resp = await anon_client.get(
            "/v1/auth/oidc/callback",
            params={"error": "access_denied", "state": state})
        assert _fragment(resp.headers["location"])["error"] == "exchange_failed"

    async def test_bad_nonce_is_rejected(
            self, oidc_app, anon_client, idp, auth_service):
        state, _ = await _start_login(anon_client, idp, auth_service)
        idp.nonce = "some-other-nonce"
        resp = await anon_client.get(
            "/v1/auth/oidc/callback", params={"code": "abc", "state": state})
        assert _fragment(resp.headers["location"])["error"] == "exchange_failed"

    async def test_username_conflict_never_merges(
            self, oidc_app, anon_client, idp, auth_service):
        await auth_service.create_user("alice", "localpw")
        state, _ = await _start_login(anon_client, idp, auth_service)
        resp = await anon_client.get(
            "/v1/auth/oidc/callback", params={"code": "abc", "state": state})
        assert _fragment(resp.headers["location"])["error"] == "conflict"
        # 本地账号未被接管
        assert await auth_service.authenticate("alice", "localpw") is not None

    async def test_disabled_user_gets_disabled_code(
            self, oidc_app, anon_client, idp, auth_service):
        user = await auth_service.resolve_oidc_user(
            issuer=ISSUER, subject="subject-1", username="alice",
            display_name="Alice", role="user", auto_provision=True)
        await auth_service.update_user(user["id"], disabled=True)
        state, _ = await _start_login(anon_client, idp, auth_service)
        resp = await anon_client.get(
            "/v1/auth/oidc/callback", params={"code": "abc", "state": state})
        assert _fragment(resp.headers["location"])["error"] == "disabled"

    async def test_group_claim_maps_to_role(
            self, api_app, anon_client, idp, auth_service):
        await _attach(api_app, idp, role_map={"trove-admins": "admin"})
        idp.token_claims = {"groups": ["trove-admins"]}
        state, _ = await _start_login(anon_client, idp, auth_service)
        resp = await anon_client.get(
            "/v1/auth/oidc/callback", params={"code": "abc", "state": state})
        token = _fragment(resp.headers["location"])["token"]
        me = await anon_client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.json()["user"]["role"] == "admin"

    async def test_success_is_audited_without_secrets(
            self, oidc_app, anon_client, idp, auth_service):
        state, _ = await _start_login(anon_client, idp, auth_service)
        resp = await anon_client.get(
            "/v1/auth/oidc/callback", params={"code": "abc", "state": state})
        token = _fragment(resp.headers["location"])["token"]
        events = await auth_service.list_audit(action="auth.oidc.login")
        assert len(events) == 1
        entry = events[0]
        assert entry["status"] == 200
        assert token not in str(entry["details"])
        assert "abc" not in str(entry["details"])
