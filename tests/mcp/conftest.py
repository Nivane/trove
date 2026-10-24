"""Shared MCP fixtures.

The components fixture is a real (mocked-LLM) app: a queryable ``test_db`` with
a deterministic semantic model, so tool behaviour is exercised end to end
without network or API keys.
"""

from __future__ import annotations

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
