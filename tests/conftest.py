"""Shared test fixtures.

CI constraints (per requirements spec C6):
  - Zero API keys
  - Zero pre-existing data
  - Zero network

All LLM calls are mocked; all databases are in-memory SQLite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trove.core.config import AgentConfig
from trove.core.types import DatasourceConfig
from trove.llm.gateway import LLMGateway
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.datasource.catalog import CatalogService
from trove.storage.session_store import SessionStore


# ── Fixtures ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_trace_store():
    """trace store 是进程级全局;每个测试从"未配置"状态开始,
    避免其它测试文件 configure_trace_store 的残留(如激活 RunTracer)。"""
    from trove.tracing.local import configure_trace_store
    configure_trace_store(None)
    yield


@pytest.fixture
def tmp_home(tmp_path):
    """A temporary trove home directory."""
    home = tmp_path / "trove_home"
    home.mkdir(exist_ok=True)
    return home


@pytest.fixture
def mock_llm():
    """LLM gateway that returns canned responses (no network)."""
    return LLMGateway(mock_response="OK")


@pytest.fixture
def sql_llm():
    """LLM gateway that returns SQL in mock responses."""
    return LLMGateway(mock_response="```sql\nSELECT 1;\n```")


@pytest.fixture
async def sqlite_registry():
    """Connected in-memory SQLite registry with a test table."""
    registry = ConnectorRegistry()
    config = DatasourceConfig(
        name="test_db",
        type="sqlite",
        connection_params={"path": ":memory:"},
        default=True,
    )
    adapter = await registry.register(config, set_default=True)

    # Create a test table
    await adapter.execute(
        "CREATE TABLE students ("
        "id INTEGER PRIMARY KEY, "
        "name TEXT, "
        "grade INTEGER, "
        "county TEXT)"
    )
    await adapter.execute(
        "INSERT INTO students (name, grade, county) VALUES "
        "('Alice', 95, 'Alameda'), "
        "('Bob', 88, 'Alameda'), "
        "('Carol', 92, 'Orange'), "
        "('Dave', 75, 'Orange'), "
        "('Eve', 99, 'Los Angeles')"
    )

    yield registry
    await registry.close_all()


@pytest.fixture
async def demo_registry(tmp_home):
    """Registry with the BIRD financial demo dataset."""
    from trove.demo import create_demo_database
    from trove.services.datasource.adapters.sqlite import SQLiteAdapter

    registry = ConnectorRegistry()
    adapter = SQLiteAdapter(name="demo", config={"path": str(tmp_home / "demo.db")})
    await adapter.connect()
    await create_demo_database(adapter)
    await adapter.disconnect()

    config = DatasourceConfig(
        name="demo",
        type="sqlite",
        connection_params={"path": str(tmp_home / "demo.db")},
        default=True,
    )
    await registry.register(config, set_default=True)

    yield registry
    await registry.close_all()


@pytest.fixture
def catalog(sqlite_registry):
    """Catalog service bound to the sqlite_registry fixture."""
    return CatalogService(sqlite_registry)


@pytest.fixture
def agent_config(tmp_home):
    """AgentConfig with test values."""
    config = AgentConfig(home=str(tmp_home), target="mock/model")
    return config


class ScriptedGateway:
    """LLM gateway mock: scripted responses; StopIteration if called too often."""

    def __init__(self, responses):
        self._responses = iter(responses)

    async def chat(self, model, messages, **kwargs):
        return next(self._responses)

    async def chat_full(self, model, messages, tools=None, **kwargs):
        return {"content": next(self._responses), "tool_calls": []}


@pytest.fixture
def graphs(sqlite_registry, agent_config):
    """Compiled LangGraph graphs (reflection/fixed/empty) with mock LLM."""
    from trove.workflow.graphs import GraphServices, build_graphs

    services = GraphServices(
        llm=ScriptedGateway(["query", "```sql\nSELECT name FROM students;\n```", "OK"]),
        catalog=CatalogService(sqlite_registry),
        connectors=sqlite_registry,
        config=agent_config,
    )
    return build_graphs(services, multi_candidate=False, planner=False, agentic=False)


@pytest.fixture
async def session_manager(tmp_home, agent_config, graphs):
    """Fully wired SessionManager for integration tests."""
    from trove.agent.session import SessionManager

    store = SessionStore(home_dir=str(tmp_home))
    llm = ScriptedGateway(["query", "```sql\nSELECT name FROM students;\n```", "OK"])

    manager = SessionManager(
        config=agent_config,
        session_store=store,
        graphs=graphs,
        llm_gateway=llm,
    )
    return manager
