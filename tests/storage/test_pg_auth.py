"""AuthService on real PostgreSQL — env-gated integration test.

    PG_TEST_URL=postgresql://trove:trove@localhost:5432/trove \
        uv run pytest -m integration tests/storage/test_pg_auth.py

回归守卫:``_normalize_ddl`` 曾把 ``INTEGER PRIMARY KEY AUTOINCREMENT`` 翻成
不带 PK 的 IDENTITY,``tokens``/``user_datasources`` 的 ``REFERENCES users(id)``
在 PG 上因此建不出来(42830)——SQLite 测试与既有 PG 集成集都无感。这里在真
PG 上把 auth schema 初始化 + 建号 + 认证 + 授权读写全链路钉住。
"""

from __future__ import annotations

import os

import pytest

PG_URL = os.environ.get("PG_TEST_URL")

pytestmark = pytest.mark.integration


@pytest.fixture
def storage_url() -> str:
    if not PG_URL:
        pytest.skip("PG_TEST_URL not set")
    return PG_URL


async def test_auth_roundtrip_on_pg(storage_url, monkeypatch, tmp_path):
    monkeypatch.setenv("TROVE_STORAGE_URL", storage_url)
    from trove.services.auth.service import AuthService

    svc = AuthService(tmp_path / "auth.db")
    try:
        # schema(users/tokens/user_datasources/audit/login_attempts)能建出来,
        # 这一步就是回归本体:tokens 的 REFERENCES users(id) 在 PK 丢失时 42830。
        created = await svc.create_user("alice", "pw123")
        authed = await svc.authenticate("alice", "pw123")
        assert authed and authed["id"] == created["id"]

        await svc.set_datasources(authed["id"], ["demo", "other"])
        users, total = await svc.list_users_page()
        assert total == 1
        assert users[0]["datasources"] == ["demo", "other"]
    finally:
        await svc.store.dispose()
