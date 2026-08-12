"""Shared test fixtures.

CI constraints (per requirements spec C6):
  - Zero API keys
  - Zero pre-existing data
  - Zero network

All LLM calls are mocked; all databases are in-memory SQLite.
"""

from __future__ import annotations

import asyncio
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
from trove.workflow.engine import WorkflowEngine
from trove.workflow.registry import WorkflowRegistry
from trove.agent.session import SessionManager


# ── Fixtures ─────────────────────────────────────────────


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
def engine():
    """Workflow engine with all built-in workflows registered."""
    eng = WorkflowEngine()
    for name in WorkflowRegistry.list_available():
        eng.register(WorkflowRegistry.create(name))
    return eng


@pytest.fixture
def agent_config(tmp_home):
    """AgentConfig with test values and injected services."""
    config = AgentConfig(home=str(tmp_home), target="mock/model")
    return config


@pytest.fixture
async def session_manager(
    tmp_home,
    agent_config,
    engine,
    sqlite_registry,
):
    """Fully wired SessionManager for integration tests."""
    store = SessionStore(home_dir=str(tmp_home))
    catalog = CatalogService(sqlite_registry)
    llm = LLMGateway(mock_response="```sql\nSELECT name FROM students;\n```")

    manager = SessionManager(
        config=agent_config,
        session_store=store,
        workflow_engine=engine,
        llm_gateway=llm,
        catalog_service=catalog,
        connector_registry=sqlite_registry,
    )
    return manager
