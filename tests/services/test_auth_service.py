"""AuthService unit tests — zero network, in-memory/`tmp_path` app.db."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trove.core.errors import AuthError
from trove.services.auth.passwords import hash_password, verify_password
from trove.services.auth.service import AuthService


@pytest.fixture
async def auth(tmp_path):
    return AuthService(tmp_path / "app.db")


# ── Passwords ─────────────────────────────────────────────


def test_hash_verify_roundtrip():
    encoded = hash_password("s3cret!")
    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password("s3cret!", encoded)
    assert not verify_password("wrong", encoded)


def test_verify_rejects_garbage():
    assert not verify_password("x", "")
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "md5$1$aa$bb")


def test_hashes_are_salted():
    a = hash_password("same")
    b = hash_password("same")
    assert a != b


# ── Bootstrap admin ───────────────────────────────────────


async def test_bootstrap_creates_admin_with_env_password(auth):
    user, initial = await auth.ensure_bootstrap_admin(env_password="envpw")
    assert user == "admin"
    assert initial == "envpw"  # first creation reports the effective password
    users = await auth.list_users()
    assert len(users) == 1
    assert users[0]["role"] == "admin"
    assert await auth.authenticate("admin", "envpw") is not None


async def test_bootstrap_generates_password_once(auth):
    user, generated = await auth.ensure_bootstrap_admin()
    assert user == "admin"
    assert generated and len(generated) >= 16
    # Idempotent: second call returns no password and keeps the account
    user2, generated2 = await auth.ensure_bootstrap_admin(env_password="other")
    assert generated2 is None
    assert await auth.authenticate("admin", generated)
    assert await auth.authenticate("admin", "other") is None


# ── Users ─────────────────────────────────────────────────


async def test_create_user_duplicate_raises(auth):
    await auth.create_user("bob", "pw1")
    with pytest.raises(AuthError) as exc:
        await auth.create_user("bob", "pw2")
    assert exc.value.code == "AUTH_006"
    assert await auth.authenticate("bob", "pw1") is not None


async def test_list_users_never_exposes_password_hash(auth):
    await auth.ensure_bootstrap_admin(env_password="x")
    await auth.create_user("bob", "pw", display_name="Bobby")
    for u in await auth.list_users():
        assert "password_hash" not in u


async def test_authenticate_disabled_user_returns_none(auth):
    u = await auth.create_user("bob", "pw")
    await auth.update_user(u["id"], disabled=True)
    assert await auth.authenticate("bob", "pw") is None


async def test_update_user_password_and_role(auth):
    u = await auth.create_user("bob", "pw")
    updated = await auth.update_user(u["id"], password="newpw", display_name="B")
    assert updated["display_name"] == "B"
    assert await auth.authenticate("bob", "newpw") is not None
    assert await auth.authenticate("bob", "pw") is None
    assert await auth.update_user(9999, display_name="x") is None


async def test_delete_user(auth):
    u = await auth.create_user("bob", "pw")
    assert await auth.delete_user(u["id"], actor_id=0)
    assert await auth.delete_user(u["id"], actor_id=0) is False


async def test_cannot_delete_self(auth):
    u = await auth.create_user("bob", "pw")
    with pytest.raises(AuthError):
        await auth.delete_user(u["id"], actor_id=u["id"])


async def test_cannot_delete_or_demote_last_admin(auth):
    await auth.ensure_bootstrap_admin(env_password="x")
    admin = await auth.authenticate("admin", "x")
    assert admin is not None
    with pytest.raises(AuthError):
        await auth.delete_user(admin["id"], actor_id=999)
    with pytest.raises(AuthError):
        await auth.update_user(admin["id"], role="user")


async def test_can_delete_admin_when_another_exists(auth):
    await auth.ensure_bootstrap_admin(env_password="x")
    admin = await auth.authenticate("admin", "x")
    other = await auth.create_user("admin2", "pw", role="admin")
    # Two admins: one may be deleted, the remaining one becomes protected
    assert await auth.delete_user(other["id"], actor_id=999)
    with pytest.raises(AuthError):
        await auth.delete_user(admin["id"], actor_id=999)


# ── Tokens ────────────────────────────────────────────────


async def test_token_roundtrip(auth):
    u = await auth.create_user("bob", "pw")
    raw, record = await auth.create_token(u["id"], label="cli")
    assert raw.startswith("trove_")
    resolved = await auth.resolve_token(raw)
    assert resolved["id"] == u["id"]
    assert resolved["username"] == "bob"
    assert await auth.revoke_token(record["id"])
    assert await auth.resolve_token(raw) is None


async def test_token_expiry(auth):
    u = await auth.create_user("bob", "pw")
    raw, _ = await auth.create_token(u["id"], ttl_hours=0)  # already expired
    assert await auth.resolve_token(raw) is None


async def test_token_invalid_and_unknown(auth):
    assert await auth.resolve_token("") is None
    assert await auth.resolve_token("trove_bogus") is None


async def test_token_rejected_for_disabled_user(auth):
    u = await auth.create_user("bob", "pw")
    raw, _ = await auth.create_token(u["id"])
    await auth.update_user(u["id"], disabled=True)
    assert await auth.resolve_token(raw) is None


async def test_list_tokens_metadata_only(auth):
    u = await auth.create_user("bob", "pw")
    await auth.create_token(u["id"], label="a")
    await auth.create_token(u["id"], label="b")
    tokens = await auth.list_tokens(u["id"])
    assert len(tokens) == 2
    assert all("token_hash" not in t or t["token_hash"] for t in tokens)


# ── Login rate limiting ───────────────────────────────────


async def _insert_old_attempt(auth, username: str, ts: str) -> None:
    conn = await auth.store._conn()
    try:
        await conn.execute(
            "INSERT INTO login_attempts (username, ip, success, ts) "
            "VALUES (?, '', 0, ?)",
            (username, ts),
        )
        await conn.commit()
    finally:
        await conn.close()


async def test_login_attempts_below_limit_allowed(auth):
    for _ in range(4):
        await auth.record_login_attempt("bob", "1.2.3.4", success=False)
    allowed, retry_after = await auth.login_attempt_allowed("bob")
    assert allowed is True
    assert retry_after == 0


async def test_login_attempts_exceed_limit_blocked(auth):
    for _ in range(5):
        await auth.record_login_attempt("bob", "1.2.3.4", success=False)
    allowed, retry_after = await auth.login_attempt_allowed("bob")
    assert allowed is False
    assert 0 < retry_after <= 15 * 60


async def test_login_attempts_window_slides(auth):
    old = (
        datetime.now(timezone.utc) - timedelta(minutes=20)
    ).isoformat()
    for _ in range(5):
        await _insert_old_attempt(auth, "bob", old)
    allowed, _ = await auth.login_attempt_allowed("bob")
    assert allowed is True  # all 5 failures aged out of the window


async def test_login_success_clears_failures(auth):
    for _ in range(5):
        await auth.record_login_attempt("bob", "1.2.3.4", success=False)
    await auth.record_login_attempt("bob", "1.2.3.4", success=True)
    allowed, _ = await auth.login_attempt_allowed("bob")
    assert allowed is True


async def test_purge_expired_tokens(auth):
    u = await auth.create_user("bob", "pw")
    expired, _ = await auth.create_token(u["id"], ttl_hours=0)  # already expired
    live, _ = await auth.create_token(u["id"])  # no TTL → never expires
    purged = await auth.purge_expired_tokens()
    assert purged == 1
    assert await auth.resolve_token(expired) is None
    assert await auth.resolve_token(live) is not None


async def test_purge_old_login_attempts(auth):
    old = (
        datetime.now(timezone.utc) - timedelta(minutes=60)
    ).isoformat()
    await _insert_old_attempt(auth, "bob", old)
    await auth.record_login_attempt("bob", "1.2.3.4", success=False)
    purged = await auth.purge_old_login_attempts()
    assert purged == 1
    allowed, _ = await auth.login_attempt_allowed("bob")
    assert allowed is True  # only the fresh failure remains, below the limit


async def test_token_ttl_hours_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TROVE_TOKEN_TTL_HOURS", "1")
    service = AuthService(tmp_path / "env_app.db")
    assert service.token_ttl_hours == 1
    u = await service.create_user("bob", "pw")
    raw, record = await service.create_token(
        u["id"], label="login", ttl_hours=service.token_ttl_hours
    )
    expires = datetime.fromisoformat(record["expires_at"])
    remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    assert 0 < remaining <= 3600
    assert await service.resolve_token(raw) is not None


# ── Datasource grants ─────────────────────────────────────


async def test_datasource_grants(auth):
    u = await auth.create_user("bob", "pw")
    assert await auth.get_datasources(u["id"]) == []
    await auth.set_datasources(u["id"], ["financial", "sales"])
    assert await auth.get_datasources(u["id"]) == ["financial", "sales"]
    await auth.set_datasources(u["id"], [])
    assert await auth.get_datasources(u["id"]) == []


# ── Audit ─────────────────────────────────────────────────


async def test_audit_append_and_list(auth):
    u = await auth.create_user("bob", "pw")
    await auth.record_audit("auth.login", user=u, method="POST", path="/v1/auth/login", status=200)
    await auth.record_audit("auth.login", status=401, details={"reason": "bad password"})
    await auth.record_audit("admin.user.create", user=u, status=201)

    entries = await auth.list_audit()
    assert len(entries) == 3
    assert entries[0]["action"] == "admin.user.create"
    assert entries[0]["username"] == "bob"

    by_user = await auth.list_audit(user_id=u["id"])
    assert len(by_user) == 2
    assert all(e["user_id"] == u["id"] for e in by_user)

    logins = await auth.list_audit(action="auth.login")
    assert len(logins) == 2
    anonymous = [e for e in logins if e["user_id"] is None]
    assert len(anonymous) == 1
    assert anonymous[0]["details"] == {"reason": "bad password"}
    assert all(e["user_id"] == u["id"] for e in logins if e["user_id"] is not None)
