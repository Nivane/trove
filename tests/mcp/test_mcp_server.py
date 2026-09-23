"""Trove MCP server 测试:ask_data / list_datasources / kb_status / 多轮会话。"""

from __future__ import annotations

import asyncio
import json

import pytest

from trove.core.config import AgentConfig
from trove.services.kb.service import KbService
from trove.storage.session_store import SessionStore


@pytest.fixture
async def mcp_components(tmp_path, sqlite_registry):
    """真实 components(可答 students 的 graph + 可查状态的 KB)。"""
    from tests.conftest import ScriptedGateway, make_test_semantic_provider

    from trove.workflow.graphs import GraphServices, build_graphs

    kb = KbService(tmp_path / "kb")
    # 用与 fixture 相同的确定性语义模型写入 kb(供 kb_status/list_datasources)
    await make_test_semantic_provider(sqlite_registry, tmp_path / "kb")

    config = AgentConfig(home=str(tmp_path / "home"), target="mock/model")
    llm = ScriptedGateway([
        "query", "```sql\nSELECT name FROM students;\n```", "OK",
        "query", "```sql\nSELECT name FROM students;\n```", "OK",
    ])
    services = GraphServices(
        llm=llm,
        connectors=sqlite_registry,
        semantic_layer=getattr(sqlite_registry, "_test_semantic_provider", None),
        kb=kb,
        config=config,
    )
    from trove.agent.session import SessionManager

    manager = SessionManager(
        config=config,
        session_store=SessionStore(home_dir=str(tmp_path / "home")),
        graphs=build_graphs(services, multi_candidate=False, query_sketch=False, agentic=False),
        llm_gateway=llm,
        kb=kb,
        connectors=sqlite_registry,
    )
    try:
        yield {
            "session_manager": manager,
            "connector_registry": sqlite_registry,
            "kb": kb,
            "config": config,
        }
    finally:
        await manager.dispose()


async def _invoke(server, name: str, **args):
    """直接调用工具底层函数(fastmcp call_tool 需要 request context,测试用 fn 直调)。

    工具函数可能 async(ask_data)或 sync(kb_status/list_datasources)。
    """
    tool = await server.get_tool(name)
    result = tool.fn(**args)
    if asyncio.iscoroutine(result):
        return await result
    return result


async def test_list_tools(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert {"ask_data", "list_datasources", "kb_status"} <= names


async def test_ask_data_answers(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(server, "ask_data",
        question="What students are in Alameda county?", datasource="test_db")
    assert isinstance(payload, dict)
    assert payload["session_id"]
    assert payload["sql"]
    assert payload["row_count"] == 5
    assert payload["no_model"] is False


async def test_ask_data_multi_turn_session(mcp_components):
    """同 session_id 复用会话:第二问能看到历史(session 保持同 id)。"""
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    r1 = await _invoke(server, "ask_data",
        question="What students are in Alameda county?", datasource="test_db")
    sid = r1["session_id"]
    r2 = await _invoke(server, "ask_data",
        question="What students are in Orange county?", datasource="test_db", session_id=sid)
    assert r2["session_id"] == sid  # 同一会话被复用
    assert r2["sql"]


async def test_ask_data_requires_question(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(server, "ask_data", question="   ")
    assert "error" in payload


async def test_list_datasources_only_initialized(mcp_components, tmp_path):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(server, "list_datasources")
    names = {d["name"] for d in payload["datasources"]}
    # test_db 有 semantics.yml → 可见;无语义模型的源不可见
    assert "test_db" in names
    assert all(d.get("has_semantics") for d in payload["datasources"])


async def test_kb_status(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    ok = await _invoke(server, "kb_status", datasource="test_db")
    assert ok["connected"] is True
    assert ok["has_semantics"] is True

    missing = await _invoke(server, "kb_status", datasource="nope")
    assert missing["connected"] is False
    assert missing["kb_initialized"] is False


async def test_kb_status_requires_datasource(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(server, "kb_status", datasource="")
    assert "error" in payload


async def test_unknown_tool_errors(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    assert await server.get_tool("nope") is None


# ── resources(MCP 三原语:只读数据)────────────────────────────

async def _read(server, uri: str) -> str:
    """read_resource → 拼接 contents 文本(fastmcp ResourceResult)。"""
    result = await server.read_resource(uri)
    return "".join(
        c.content if isinstance(c.content, str) else str(c.content)
        for c in result.contents
    )


async def test_resources_registered(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    uris = {str(r.uri) for r in await server.list_resources()}
    templates = {
        str(t.uri_template) for t in await server.list_resource_templates()
    }
    assert "trove://datasources" in uris
    assert "trove://{datasource}/schema" in templates
    assert "trove://{datasource}/semantics" in templates


async def test_datasources_resource(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    text = await _read(server, "trove://datasources")
    assert "test_db" in text  # 有 semantics.yml 的源可见


async def test_semantics_resource_returns_model(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    text = await _read(server, "trove://test_db/semantics")
    assert "semantic_model" in text  # OSSIE 语义模型原文


async def test_schema_resource_missing_placeholder(mcp_components):
    """fixture 只写 semantics.yml → schema 返回明确占位而非报错。"""
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    text = await _read(server, "trove://test_db/schema")
    assert text == "(no such KB file)"


async def test_schema_resource_returns_notes(mcp_components, tmp_path):
    """写入 schema_notes.yml 后资源返回其内容(把元数据暴露成工具底座)。"""
    import yaml

    from trove.mcp.server import build_mcp_server

    ds_dir = mcp_components["kb"].kb_dir / "test_db"
    (ds_dir / "schema_notes.yml").write_text(
        yaml.safe_dump({"students": {"desc": "student records"}}),
        encoding="utf-8",
    )
    server = build_mcp_server(mcp_components)
    text = await _read(server, "trove://test_db/schema")
    assert "student records" in text


async def test_resource_blocks_path_traversal(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    # 单段危险名可寻址 → handler 拒绝
    for evil in ("..", "."):
        assert "(invalid datasource name)" == await _read(server, f"trove://{evil}/semantics")
    # 多段穿越 URI 根本不匹配模板(datasource 不能含 "/")→ 不可解析
    with pytest.raises(Exception):
        await _read(server, "trove://../secret/semantics")


# ── prompts(MCP 三原语:可复用模板)────────────────────────────

async def _render_prompt(server, name: str, **args) -> str:
    prompt = await server.get_prompt(name)
    assert prompt is not None, f"prompt {name!r} not registered"
    result = prompt.render(args)
    if asyncio.iscoroutine(result):
        result = await result
    if isinstance(result, str):
        return result
    return str(result)


async def test_prompts_registered(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    names = {p.name for p in await server.list_prompts()}
    assert {"datasource_guide", "ask_data"} <= names


async def test_datasource_guide_prompt(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    text = await _render_prompt(server, "datasource_guide", datasource="test_db")
    assert "trove://test_db/schema" in text
    assert "trove://test_db/semantics" in text
    assert "ask_data" in text


async def test_datasource_guide_blocks_unsafe_name(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    text = await _render_prompt(server, "datasource_guide", datasource="..")
    assert "invalid datasource name" in text


async def test_ask_data_prompt(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    text = await _render_prompt(
        server, "ask_data", datasource="test_db", question="有多少学生?",
    )
    assert "test_db" in text and "有多少学生?" in text


# ── 权限:非 admin identity 按数据源 grants 过滤 ──────────────────

class _FakeAuth:
    """最小 auth 替身:get_datasources 按 user_id 返回 grants。"""

    def __init__(self, grants_by_user: dict):
        self._grants = grants_by_user

    async def get_datasources(self, user_id):
        return list(self._grants.get(user_id, []))


def _server_with_identity(components, auth, identity):
    from trove.mcp.server import build_mcp_server

    return build_mcp_server({**components, "auth": auth}, identity=identity)


async def test_non_admin_with_grants_can_query_granted_only(mcp_components):
    """非 admin 身份:ask_data 只能查 grants 允许的数据源。"""
    auth = _FakeAuth({1: ["test_db"]})
    server = _server_with_identity(
        mcp_components, auth, {"id": 1, "role": "user", "username": "bob"},
    )
    ok = await _invoke(server, "ask_data",
        question="What students are in Alameda county?", datasource="test_db")
    assert ok["sql"] and ok["row_count"] == 5

    blocked = await _invoke(server, "ask_data",
        question="count rows", datasource="other_db")
    assert blocked.get("error") == "datasource not allowed"


async def test_non_admin_list_scoped_by_grants(mcp_components):
    """list_datasources / datasources_resource 只列 grants 内的源。"""
    auth = _FakeAuth({1: ["test_db"]})
    server = _server_with_identity(
        mcp_components, auth, {"id": 1, "role": "user", "username": "bob"},
    )
    payload = await _invoke(server, "list_datasources")
    assert {d["name"] for d in payload["datasources"]} == {"test_db"}

    text = await _read(server, "trove://datasources")
    assert "test_db" in text


async def test_non_admin_kb_status_and_resources_gated(mcp_components):
    auth = _FakeAuth({1: ["test_db"]})
    server = _server_with_identity(
        mcp_components, auth, {"id": 1, "role": "user", "username": "bob"},
    )
    # grants 内 → 正常状态
    ok = await _invoke(server, "kb_status", datasource="test_db")
    assert ok["connected"] is True
    # grants 外 → 拒绝而非泄露状态
    denied = await _invoke(server, "kb_status", datasource="other_db")
    assert "not allowed" in denied.get("error", "")
    text = await _read(server, "trove://other_db/semantics")
    assert "datasource not allowed" in text


async def test_admin_identity_unrestricted(mcp_components):
    """admin identity:与无身份一致,全量可见可答。"""
    auth = _FakeAuth({1: []})
    server = _server_with_identity(
        mcp_components, auth, {"id": 1, "role": "admin", "username": "root"},
    )
    payload = await _invoke(server, "list_datasources")
    assert "test_db" in {d["name"] for d in payload["datasources"]}
    ok = await _invoke(server, "ask_data",
        question="What students are in Alameda county?", datasource="test_db")
    assert ok["sql"] and ok["row_count"] == 5


async def test_empty_grants_only_default_datasource(mcp_components):
    """空 grants(非 admin)→ 只放行默认数据源(与 require_datasource 同语义)。"""
    auth = _FakeAuth({1: []})
    server = _server_with_identity(
        mcp_components, auth, {"id": 1, "role": "user", "username": "bob"},
    )
    # 省略 datasource → 默认源
    ok = await _invoke(server, "ask_data",
        question="What students are in Alameda county?")
    assert ok["sql"] and ok["row_count"] == 5
    # 显式指定非默认源 → 拒绝
    blocked = await _invoke(server, "ask_data",
        question="count rows", datasource="other_db")
    assert blocked.get("error") == "datasource not allowed"


async def test_identity_without_auth_default_only(mcp_components):
    """有身份但缺 auth 服务 → 按"空 grants"处理:只放行默认源。"""
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components, identity={"id": 1, "role": "user"})
    payload = await _invoke(server, "list_datasources")
    assert {d["name"] for d in payload["datasources"]} == {"test_db"}
    ok = await _invoke(server, "ask_data",
        question="What students are in Alameda county?")
    assert ok["sql"] and ok["row_count"] == 5
    blocked = await _invoke(server, "ask_data",
        question="count rows", datasource="other_db")
    assert blocked.get("error") == "datasource not allowed"


# ── 通道鉴权:trove mcp --token 解析(fail-closed)────────────────

class _TokenAuth:
    async def resolve_token(self, raw):
        return {"id": 1, "role": "admin"} if raw == "valid" else None


async def test_mcp_identity_for():
    from trove.main import _mcp_identity_for

    # stdio 本地挂载 → 无身份(不设限)
    assert await _mcp_identity_for("stdio", "127.0.0.1", None, None) is None
    # loopback + 无 token → 允许(本地开发),但不解析身份
    assert await _mcp_identity_for("sse", "127.0.0.1", None, None) is None
    # 非 loopback 网络绑定 + 无 token → 拒绝(fail-closed)
    with pytest.raises(SystemExit):
        await _mcp_identity_for("sse", "0.0.0.0", None, None)
    # 无效 token → 拒绝
    with pytest.raises(SystemExit):
        await _mcp_identity_for("sse", "0.0.0.0", "bad", _TokenAuth())
    # 有效 token → 解析出身份(供 grants 校验)
    ident = await _mcp_identity_for("sse", "0.0.0.0", "valid", _TokenAuth())
    assert ident == {"id": 1, "role": "admin"}
    # loopback + token → 同样解析身份(可选鉴权)
    assert await _mcp_identity_for("sse", "127.0.0.1", "valid", _TokenAuth()) is not None
