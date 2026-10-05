"""AuthService unit tests — zero network, in-memory/`tmp_path` app.db."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trove.core.errors import AuthError
from trove.services.auth.passwords import hash_password, verify_password
from trove.services.auth.service import AuthService
from trove.services.auth.store import AppDbStore


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


async def test_token_scopes_roundtrip(auth):
    """受限 token:scopes 存/取一致,resolve 附到用户 dict。"""
    u = await auth.create_user("bob", "pw")
    raw, record = await auth.create_token(u["id"], label="query-only", scopes=["query"])
    assert record["scopes"] == ["query"]
    resolved = await auth.resolve_token(raw)
    assert resolved["scopes"] == ["query"]

    listed = await auth.list_tokens(u["id"])
    assert listed[0]["scopes"] == ["query"]


async def test_token_scopes_default_unrestricted(auth):
    """未声明 scopes 的存量 token = 不限(空列表),行为不变。"""
    u = await auth.create_user("bob", "pw")
    raw, record = await auth.create_token(u["id"], label="legacy")
    assert record["scopes"] == []
    resolved = await auth.resolve_token(raw)
    assert resolved["scopes"] == []


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


# ── Topic grants(三态往返:None / {} / 清单)───────────────


async def test_topic_grants_three_states_roundtrip(auth):
    """``None``(未收窄)与 ``{}``(收窄到零)是两种语义,存储层得分得开。"""
    u = await auth.create_user("bob", "pw")
    assert await auth.get_topic_grants(u["id"]) is None   # 新用户 = 未配置

    await auth.set_topic_grants(u["id"], {"financial": ["loans", "cards"]})
    assert await auth.get_topic_grants(u["id"]) == {
        "financial": ["cards", "loans"]}   # 写入侧排序,读回即唯一形态

    await auth.set_topic_grants(u["id"], {})
    assert await auth.get_topic_grants(u["id"]) == {}     # 不是 None

    await auth.set_topic_grants(u["id"], None)
    assert await auth.get_topic_grants(u["id"]) is None   # 取消收窄


async def test_topic_grants_are_normalized_on_write(auth):
    """归一化在写入侧一次完成:空白裁剪 / 去重 / 排序,读回即唯一形态。"""
    u = await auth.create_user("bob", "pw")
    await auth.set_topic_grants(u["id"], {
        " financial ": ["loans", "loans", " cards ", "  "],
        "   ": ["ghost"],           # 空源名整条丢弃
    })
    assert await auth.get_topic_grants(u["id"]) == {
        "financial": ["cards", "loans"]}


async def test_topic_grants_per_datasource_empty_list_survives(auth):
    """``{ds: []}``(该源无域)与 ``{}``(全无域)都必须原样存活 ——
    塌成同一形态会让某一种语义悄悄变成另一种。"""
    u = await auth.create_user("bob", "pw")
    await auth.set_topic_grants(u["id"], {"financial": []})
    assert await auth.get_topic_grants(u["id"]) == {"financial": []}


async def test_topic_grants_malformed_storage_reads_as_deny_all(auth):
    """人工改库改坏的字节 → 空字典(一个域都不见),**不是** None。

    None 在域层是"未收窄";把读不懂的东西翻译成放行是最坏的降级方向。
    空串也算"读不懂":自家写入器只落 NULL 或合法 JSON(None 走 NULL),
    ``''`` 只可能来自库外的手。
    """
    u = await auth.create_user("bob", "pw")
    await auth.store._backend.execute(
        "UPDATE users SET topic_grants_json = ? WHERE id = ?",
        ('{"financial": "loans"}', u["id"]))   # 值不是列表
    await auth.store._backend.commit()
    assert await auth.get_topic_grants(u["id"]) == {}

    await auth.store._backend.execute(
        "UPDATE users SET topic_grants_json = ? WHERE id = ?",
        ("not json at all", u["id"]))
    await auth.store._backend.commit()
    assert await auth.get_topic_grants(u["id"]) == {}

    await auth.store._backend.execute(
        "UPDATE users SET topic_grants_json = ? WHERE id = ?",
        ("", u["id"]))                          # 空串:同样从严
    await auth.store._backend.commit()
    assert await auth.get_topic_grants(u["id"]) == {}


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


async def test_aggregate_query_audit_dedupes_filters_and_keeps_privacy(tmp_path):
    """历史蒸馏的审计聚合读:按 (question, sql) 去重计数,产物不带用户归属。"""
    store = AppDbStore(tmp_path / "app.db")
    ts = datetime.now(timezone.utc).isoformat()
    details = {"question": "哪个地区贷款金额最高?", "sql": "SELECT 1",
               "verdict": "OK", "datasource": "demo"}
    for username in ("alice", "bob"):
        await store.append_audit(ts=ts, user_id=None, username=username,
                                 action="query.execute", details=details)
    await store.append_audit(ts=ts, user_id=None, username="bob",
                             action="query.execute",
                             details={"question": "其他", "sql": "SELECT 2",
                                      "datasource": "other"})
    await store.append_audit(ts=ts, user_id=None, username="bob",
                             action="auth.login", details=details)

    rows = await store.aggregate_query_audit(datasource="demo")
    assert len(rows) == 1
    row = rows[0]
    assert row["question"] == "哪个地区贷款金额最高?"
    assert row["seen"] == 2 and row["last_seen"] == ts
    # 隐私边界:它回答「问过什么」,不回答「谁问的」
    assert "username" not in row and "user_id" not in row
    # since 只用字符串比较(库里的时间只有一种格式)
    assert await store.aggregate_query_audit(
        datasource="demo", since="2999-01-01T00:00:00+00:00") == []
    # 不限定数据源 → 两个数据源各一条;非 query.execute 的动作不算
    assert len(await store.aggregate_query_audit()) == 2


async def test_aggregate_refusal_audit_counts_window_and_keeps_privacy(tmp_path):
    """拒绝频率报表的审计读:total 精确、entries 带 miss 细节、不带用户归属。"""
    store = AppDbStore(tmp_path / "app.db")
    ts = datetime.now(timezone.utc).isoformat()
    await store.append_audit(
        ts=ts, user_id=7, username="alice", action="query.refused",
        details={"question": "各分行存款余额", "datasource": "demo",
                 "reason": "uncovered", "miss_reason": "unknown_cardinality",
                 "miss_component": "account, loan", "run_id": "r1"})
    await store.append_audit(
        ts=ts, user_id=None, username="bob", action="query.refused",
        details={"question": "哪个地区贷款最高?", "datasource": "demo",
                 "reason": "uncovered", "miss_reason": "no_metric_match"})
    # 非拒绝动作 + 坏 JSON + 远古行:都不该混进来
    await store.append_audit(ts=ts, user_id=None, username="bob",
                             action="query.execute",
                             details={"question": "q", "sql": "SELECT 1"})

    async def _bad_row():
        conn = await store._conn()
        try:
            await conn.execute(
                "INSERT INTO audit_log (ts, user_id, username, action, method,"
                " path, status, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("2020-01-01T00:00:00+00:00", None, "bob", "query.refused",
                 "", "", 200, "{not json"))
            await conn.commit()
        finally:
            await conn.close()

    await _bad_row()
    await store.append_audit(
        ts="2000-01-01T00:00:00+00:00", user_id=None, username="bob",
        action="query.refused",
        details={"question": "远古", "datasource": "demo", "reason": "no_model"})

    data = await store.aggregate_refusal_audit()
    assert data["total"] == 4  # 远古行也计数(窗口只在调用方给 since 时生效)
    assert data["capped"] is False
    # 坏 JSON 行被跳过:entries 少一行,但那行的计数仍在 total 里
    assert len(data["entries"]) == 3
    first = data["entries"][0]
    assert first["question"] == "哪个地区贷款最高?"  # ts 相同 → id DESC 稳定序
    assert first["miss_reason"] == "no_metric_match"
    assert first["reason"] == "uncovered"
    # 隐私边界:它回答「问过什么被拒了」,不回答「谁被拒了」
    assert "username" not in first and "user_id" not in first

    # since 过滤(字符串比较)与 capped 下界语义
    assert (await store.aggregate_refusal_audit(
        since="2999-01-01T00:00:00+00:00"))["total"] == 0
    capped = await store.aggregate_refusal_audit(scan_limit=1)
    assert capped["capped"] is True and len(capped["entries"]) == 1
    assert capped["total"] == 4  # total 仍精确
