"""OIDC SSO — 纯客户端 + 账号 provisioning(零真实网络)。

discovery / JWKS / token 端点全部由 ``httpx.MockTransport`` 提供,ID token
用本地生成的 RSA 密钥签 —— 与生产同一条 PyJWT 校验路径,但不碰网络。
"""

from __future__ import annotations

import base64
import hashlib
import time

import httpx
import jwt
import pytest

from tests.helpers.oidc import CLIENT_ID, ISSUER, FakeIdp, make_key
from trove.core.config import OidcConfig
from trove.services.auth.oidc import (
    OidcClient,
    OidcError,
    new_pkce,
    sanitize_next,
    validate_oidc_config,
)
from trove.services.auth.service import (
    OIDC_PASSWORD_SENTINEL,
    OIDC_ROLE_RANK,
    AuthService,
    map_oidc_role,
)


def _client(idp: FakeIdp, **cfg) -> OidcClient:
    config = OidcConfig(enabled=True, issuer=ISSUER, client_id=CLIENT_ID, **cfg)
    return OidcClient(config, http=httpx.AsyncClient(transport=idp.transport))


# ── Config validation ─────────────────────────────────────


class TestValidateConfig:
    def test_valid_config_has_no_problems(self):
        cfg = OidcConfig(
            enabled=True, issuer=ISSUER, client_id=CLIENT_ID,
            client_secret_ref="env:OIDC_SECRET",
            redirect_uri="https://trove.example.com/v1/auth/oidc/callback",
            role_map={"admins": "admin"},
        )
        assert validate_oidc_config(cfg) == []

    def test_loopback_http_is_allowed(self):
        cfg = OidcConfig(enabled=True, issuer="http://127.0.0.1:9000",
                         client_id=CLIENT_ID)
        assert validate_oidc_config(cfg) == []

    def test_plain_http_and_literal_secret_are_rejected(self):
        cfg = OidcConfig(
            enabled=True, issuer="http://idp.example.com", client_id="",
            client_secret_ref="hunter2", default_role="root",
            role_map={"ops": "superuser"},
        )
        problems = validate_oidc_config(cfg)
        text = " ".join(problems)
        assert "issuer must be https" in text
        assert "client_id is required" in text
        assert "env:NAME" in text
        assert "default_role" in text
        assert "role_map" in text


# ── Small helpers ─────────────────────────────────────────


class TestHelpers:
    def test_pkce_is_s256_and_unpadded(self):
        verifier, challenge = new_pkce()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        assert challenge == expected
        assert "=" not in challenge
        assert verifier != new_pkce()[0]

    @pytest.mark.parametrize("bad", [
        "//evil.example.com", "https://evil.example.com/x",
        "javascript:alert(1)", "\\\\evil", "/ok\nHeader: x", "",
    ])
    def test_sanitize_next_rejects_open_redirects(self, bad):
        result = sanitize_next(bad)
        if bad == "":
            assert result == "/"
        elif bad == "/ok\nHeader: x":
            assert result == "/"
        else:
            assert result == "/"

    def test_sanitize_next_keeps_relative_path(self):
        assert sanitize_next("/console/decisions?tab=1") == "/console/decisions?tab=1"

    def test_map_role_highest_privilege_wins(self):
        cfg = OidcConfig(role_map={"a": "user", "b": "admin", "c": "analyst"})
        assert map_oidc_role({"groups": ["a", "c", "b"]}, cfg) == "admin"

    def test_map_role_default_and_unknown(self):
        cfg = OidcConfig(role_map={"a": "root"}, default_role="analyst")
        # 未知角色不参与判定,落到默认档
        assert map_oidc_role({"groups": ["a"]}, cfg) == "analyst"
        assert map_oidc_role({}, cfg) == "analyst"
        assert map_oidc_role({"groups": "a"}, cfg) == "analyst"

    def test_role_rank_matches_roles(self):
        assert sorted(OIDC_ROLE_RANK, key=OIDC_ROLE_RANK.get) == [
            "user", "analyst", "admin"]


# ── Client flow ───────────────────────────────────────────


class TestDiscovery:
    async def test_discovery_is_cached(self):
        idp = FakeIdp()
        client = _client(idp)
        first = await client.discovery()
        second = await client.discovery()
        assert first is second
        assert idp.calls["discovery"] == 1

    async def test_discovery_issuer_mismatch_is_rejected(self):
        idp = FakeIdp(issuer="https://other.example.com")
        client = _client(idp)
        with pytest.raises(OidcError) as err:
            await client.discovery()
        assert err.value.code == "exchange_failed"

    async def test_authorization_url_carries_pkce_and_state(self):
        idp = FakeIdp()
        client = _client(idp, scopes="openid profile")
        verifier, challenge = new_pkce()
        url = await client.authorization_url(
            state="st-1", nonce="n-1", code_challenge=challenge,
            redirect_uri="https://trove.example.com/cb")
        assert url.startswith(f"{ISSUER}/authorize?")
        assert "code_challenge_method=S256" in url
        assert f"code_challenge={challenge}" in url
        assert "state=st-1" in url
        assert "nonce=n-1" in url
        assert "scope=openid+profile" in url


class TestExchange:
    async def test_exchange_returns_id_token(self):
        idp = FakeIdp()
        client = _client(idp)
        payload = await client.exchange(
            code="abc", code_verifier="v", redirect_uri="https://x/cb")
        assert payload["id_token"]
        assert idp.calls["token"] == 1

    async def test_exchange_failure_is_exchange_failed(self):
        idp = FakeIdp()
        idp.token_status = 500
        client = _client(idp)
        with pytest.raises(OidcError) as err:
            await client.exchange(code="abc", code_verifier="v",
                                  redirect_uri="https://x/cb")
        assert err.value.code == "exchange_failed"

    async def test_exchange_without_id_token_is_rejected(self):
        idp = FakeIdp()
        idp.token_payload = {"access_token": "only"}
        client = _client(idp)
        with pytest.raises(OidcError):
            await client.exchange(code="abc", code_verifier="v",
                                  redirect_uri="https://x/cb")

    async def test_client_secret_comes_from_env_only(self, monkeypatch):
        seen: dict = {}

        def _handle(request: httpx.Request) -> httpx.Response:
            seen.update(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json={"id_token": "x"})

        config = OidcConfig(
            enabled=True, issuer=ISSUER, client_id=CLIENT_ID,
            client_secret_ref="env:TROVE_OIDC_SECRET")
        monkeypatch.setenv("TROVE_OIDC_SECRET", "s3cret")
        client = OidcClient(
            config, http=httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: _handle(r) if "/token" in str(r.url)
                else FakeIdp()._handle(r))))
        await client.exchange(code="abc", code_verifier="v",
                              redirect_uri="https://x/cb")
        assert seen["client_secret"] == "s3cret"


class TestVerifyIdToken:
    async def test_valid_token_returns_claims(self):
        idp = FakeIdp()
        idp.nonce = "n-1"
        client = _client(idp)
        claims = await client.verify_id_token(idp.sign(), nonce="n-1")
        assert claims["sub"] == "subject-1"

    async def test_wrong_nonce_is_rejected(self):
        idp = FakeIdp()
        client = _client(idp)
        with pytest.raises(OidcError) as err:
            await client.verify_id_token(idp.sign(nonce="other"), nonce="n-1")
        assert "nonce" in str(err.value)

    @pytest.mark.parametrize("overrides", [
        {"aud": "another-client"}, {"iss": "https://evil.example.com"},
        {"exp": int(time.time()) - 3600}, {"exp": "nope"},
    ])
    async def test_bad_claims_are_rejected(self, overrides):
        idp = FakeIdp()
        idp.nonce = "n-1"
        client = _client(idp)
        with pytest.raises(OidcError):
            await client.verify_id_token(
                idp.sign(**overrides), nonce="n-1")

    async def test_symmetric_and_none_algorithms_are_rejected(self):
        idp = FakeIdp()
        client = _client(idp)
        hs = jwt.encode({"sub": "x"}, "shared-secret", algorithm="HS256")
        with pytest.raises(OidcError) as err:
            await client.verify_id_token(hs, nonce="")
        assert "unsupported" in str(err.value)
        none = jwt.encode({"sub": "x"}, None, algorithm="none")
        with pytest.raises(OidcError):
            await client.verify_id_token(none, nonce="")

    async def test_unknown_kid_refreshes_jwks_once(self):
        idp = FakeIdp()
        idp.nonce = "n-1"
        client = _client(idp)
        # 先让缓存填上 k1,再轮换签名键
        await client.verify_id_token(idp.sign(), nonce="n-1")
        idp.rotate = make_key("k2")
        idp.nonce = "n-2"
        claims = await client.verify_id_token(idp.sign(), nonce="n-2")
        assert claims["sub"] == "subject-1"
        assert idp.calls["jwks"] == 2

    async def test_ambiguous_keys_without_kid_are_rejected(self):
        idp = FakeIdp()
        client = _client(idp)

        def _two_keys(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/jwks"):
                _p, other = make_key("k2")
                return httpx.Response(200, json={"keys": [idp.jwk, other]})
            return idp._handle(request)

        client._http = httpx.AsyncClient(
            transport=httpx.MockTransport(_two_keys))
        raw = jwt.encode(
            {"iss": ISSUER, "aud": CLIENT_ID, "sub": "s",
             "exp": int(time.time()) + 300, "nonce": ""},
            idp.private, algorithm="RS256")  # 无 kid
        with pytest.raises(OidcError):
            await client.verify_id_token(raw, nonce="")


# ── Provisioning(账号映射)────────────────────────────────


@pytest.fixture
async def auth(tmp_path):
    service = AuthService(tmp_path / "app.db")
    yield service
    await service.dispose()


async def _login(auth: AuthService, **kw):
    params = dict(
        issuer=ISSUER, subject="subject-1", username="alice",
        display_name="Alice", role="user", auto_provision=True,
    )
    params.update(kw)
    return await auth.resolve_oidc_user(**params)


class TestProvisioning:
    async def test_first_login_provisions_zero_grant_account(self, auth):
        user = await _login(auth)
        assert user["username"] == "alice"
        assert user["role"] == "user"
        # 哨兵密码:本地密码登录必定失败
        assert await auth.authenticate("alice", OIDC_PASSWORD_SENTINEL) is None
        assert await auth.authenticate("alice", "anything") is None
        # 零 grants
        assert await auth.get_datasources(user["id"]) == []
        assert await auth.get_row_grants(user["id"]) is None
        assert await auth.get_column_denials(user["id"]) is None

    async def test_second_login_reuses_binding_even_if_username_moves(self, auth):
        first = await _login(auth)
        again = await _login(
            auth, username="alice-renamed", display_name="Alice New")
        assert again["id"] == first["id"]
        assert again["username"] == "alice"

    async def test_username_conflict_is_not_merged(self, auth):
        await auth.create_user("alice", "localpw")
        with pytest.raises(OidcError) as err:
            await _login(auth)
        assert err.value.code == "conflict"
        # 本地账号保持原样(本地密码仍可用)
        assert await auth.authenticate("alice", "localpw") is not None

    async def test_auto_provision_off_rejects_unknown_identity(self, auth):
        with pytest.raises(OidcError) as err:
            await _login(auth, auto_provision=False)
        assert err.value.code == "not_provisioned"
        assert await auth.list_users() == []

    async def test_disabled_user_is_rejected(self, auth):
        user = await _login(auth)
        await auth.update_user(user["id"], disabled=True)
        with pytest.raises(OidcError) as err:
            await _login(auth)
        assert err.value.code == "disabled"

    async def test_role_follows_idp_groups(self, auth):
        user = await _login(auth, role="analyst")
        assert user["role"] == "analyst"
        again = await _login(auth, role="admin")
        assert again["role"] == "admin"

    async def test_last_admin_guard_keeps_role_and_audits(self, auth):
        admin = await _login(
            auth, subject="admin-1", username="sso-admin", role="admin")
        downgraded = await _login(
            auth, subject="admin-1", username="sso-admin", role="user")
        assert downgraded["role"] == "admin"  # 守卫拒绝降级,保留原角色
        events = await auth.list_audit(action="auth.oidc.role_rejected")
        assert len(events) == 1
        assert events[0]["details"]["wanted"] == "user"
        assert admin["id"] == downgraded["id"]

    async def test_stale_identity_is_recreated(self, auth):
        user = await _login(auth)
        await auth.store.delete_user(user["id"])
        again = await _login(auth)
        assert again["id"] != user["id"]
        assert again["username"] == "alice"
