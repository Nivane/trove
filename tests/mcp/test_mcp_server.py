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
