"""Per-request identity on the MCP HTTP transports.

Before this contract the resolved identity was frozen at build time: the
middleware could authenticate every request and every tool would still run as
whichever user started the process. These tests exercise the two things that
make the difference real:

1. tools read the **current** request's identity (``request.state.mcp_identity``),
   not the startup one;
2. sessions are keyed ``(owner, session_id)``, so two callers that happen to
   send the same ``session_id`` never share a session (and therefore never
   inherit each other's ``user_id`` / execution-time authorization);
3. a token-protected HTTP request without an identity fails closed instead of
   falling back to the startup identity.
"""

from __future__ import annotations

import pytest

from tests.mcp.helpers import FakeAuth, invoke, server_with_identity
from trove.core.config import AgentConfig
from trove.services.kb.service import KbService


class _FakeSessions:
    """Records session creation + asks; no graph, no LLM."""

    def __init__(self) -> None:
        self.created: list = []
        self.asks: list[dict] = []

    async def start_session(self, *, user_id="local", **_kwargs):
        from types import SimpleNamespace

        session = SimpleNamespace(
            session_id=f"s{len(self.created) + 1}", user_id=user_id,
        )
        self.created.append(session)
        return session

    async def ask(self, *, session, question, datasource=None, scopes=None,
                  **_kwargs):
        from types import SimpleNamespace

        self.asks.append({
            "user_id": session.user_id,
            "internal_session_id": session.session_id,
            "question": question,
            "datasource": datasource,
            "scopes": scopes,
        })
        return SimpleNamespace(
            final_response="answer", sql="SELECT 1", row_count=1, verdict=None,
            datasource=datasource, no_model=False, refusal=None, error="",
        )


class _Request:
    def __init__(self, identity):
        from types import SimpleNamespace

        self.state = SimpleNamespace()
        if identity is not None:
            self.state.mcp_identity = identity


class _RequestSwitch:
    """Stands in for ``fastmcp.server.dependencies.get_http_request``."""

    def __init__(self, identity=None, *, http=True):
        self.identity = identity
        self.http = http

    def __call__(self):
        if not self.http:
            raise RuntimeError("No active HTTP request found.")
        return _Request(self.identity)


@pytest.fixture
def scope_env(tmp_path, sqlite_registry):
    sessions = _FakeSessions()
    auth = FakeAuth(
        {1: [], 2: []},
        users={
            1: {"id": 1, "username": "bob", "role": "admin"},
            2: {"id": 2, "username": "carol", "role": "admin"},
        },
    )
    components = {
        "session_manager": sessions,
        "connector_registry": sqlite_registry,
        "kb": KbService(tmp_path / "kb"),
        "config": AgentConfig(home=str(tmp_path / "home")),
        "auth": auth,
    }
    return components, sessions, auth


def _admin(uid: int, scopes=None) -> dict:
    identity = {"id": uid, "role": "admin", "username": f"user{uid}"}
    if scopes is not None:
        identity["scopes"] = scopes
    return identity


async def test_same_session_id_from_two_identities_is_two_sessions(
    scope_env, monkeypatch,
):
    components, sessions, auth = scope_env
    switch = _RequestSwitch(_admin(1))
    monkeypatch.setattr("trove.mcp.server.get_http_request", switch)
    server = server_with_identity(
        components, auth, None, require_request_identity=True,
    )

    first = await invoke(
        server, "ask_data", question="q1", datasource="test_db",
        session_id="shared",
    )
    switch.identity = _admin(2)
    second = await invoke(
        server, "ask_data", question="q2", datasource="test_db",
        session_id="shared",
    )

    assert first["session_id"] == second["session_id"] == "shared"
    assert [s.user_id for s in sessions.created] == ["1", "2"], (
        "same session_id from another identity reused the first caller's session"
    )
    assert [a["user_id"] for a in sessions.asks] == ["1", "2"]
    assert first["error"] == second["error"] == ""


async def test_same_identity_reuses_its_own_session(scope_env, monkeypatch):
    components, sessions, auth = scope_env
    switch = _RequestSwitch(_admin(1))
    monkeypatch.setattr("trove.mcp.server.get_http_request", switch)
    server = server_with_identity(
        components, auth, None, require_request_identity=True,
    )

    await invoke(server, "ask_data", question="q1", datasource="test_db",
                 session_id="shared")
    switch.identity = _admin(2)
    await invoke(server, "ask_data", question="q2", datasource="test_db",
                 session_id="shared")
    switch.identity = _admin(1)
    await invoke(server, "ask_data", question="q3", datasource="test_db",
                 session_id="shared")

    assert len(sessions.created) == 2, "同一身份重问不该再建会话"
    assert sessions.asks[-1]["user_id"] == "1"
    assert sessions.asks[-1]["internal_session_id"] == sessions.created[0].session_id


async def test_scopes_travel_with_the_request_identity(scope_env, monkeypatch):
    components, sessions, auth = scope_env
    switch = _RequestSwitch(_admin(1, scopes=["pii"]))
    monkeypatch.setattr("trove.mcp.server.get_http_request", switch)
    server = server_with_identity(
        components, auth, None, require_request_identity=True,
    )

    await invoke(server, "ask_data", question="q1", datasource="test_db")
    switch.identity = _admin(2)
    await invoke(server, "ask_data", question="q2", datasource="test_db")

    assert sessions.asks[0]["scopes"] == ["pii"]
    assert sessions.asks[1]["scopes"] is None


async def test_request_without_identity_fails_closed_not_downgraded(
    scope_env, monkeypatch,
):
    components, sessions, auth = scope_env
    switch = _RequestSwitch(None)  # HTTP context, but no resolved identity
    monkeypatch.setattr("trove.mcp.server.get_http_request", switch)
    server = server_with_identity(
        components, auth, _admin(1), require_request_identity=True,
    )

    with pytest.raises(RuntimeError, match="no authenticated identity"):
        await invoke(server, "list_datasources")
    with pytest.raises(RuntimeError, match="no authenticated identity"):
        await invoke(server, "ask_data", question="q", datasource="test_db")
    assert sessions.created == [], "未鉴权请求竟然开始了会话"


async def test_missing_identity_falls_back_only_when_not_required(
    scope_env, monkeypatch,
):
    components, _sessions, auth = scope_env
    switch = _RequestSwitch(None)
    monkeypatch.setattr("trove.mcp.server.get_http_request", switch)
    server = server_with_identity(
        components, auth, _admin(1), require_request_identity=False,
    )

    payload = await invoke(server, "kb_status", datasource="test_db")
    assert payload["connected"] is True


async def test_non_http_call_keeps_the_startup_identity(scope_env, monkeypatch):
    """stdio / 测试直调没有 HTTP 上下文 → 回退启动身份(旧行为)。"""
    components, _sessions, auth = scope_env
    switch = _RequestSwitch(http=False)
    monkeypatch.setattr("trove.mcp.server.get_http_request", switch)
    server = server_with_identity(
        components, auth, _admin(1), require_request_identity=True,
    )

    payload = await invoke(server, "kb_status", datasource="test_db")
    assert payload["connected"] is True


async def test_grants_are_read_per_request(scope_env, monkeypatch):
    """授权是库里的可变状态:撤掉 grant 必须在**下一次**请求生效。"""
    components, _sessions, auth = scope_env
    auth._grants[1] = ["other_db"]
    auth._users[1] = {"id": 1, "username": "bob", "role": "user"}
    switch = _RequestSwitch({"id": 1, "role": "user"})
    monkeypatch.setattr("trove.mcp.server.get_http_request", switch)
    server = server_with_identity(
        components, auth, None, require_request_identity=True,
    )

    allowed = await invoke(server, "kb_status", datasource="other_db")
    assert "error" not in allowed
    assert allowed["connected"] is False  # 已授权,但该源没注册

    auth._grants[1] = []
    denied = await invoke(server, "kb_status", datasource="other_db")
    assert "not allowed" in denied["error"]
