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


# ── 语义优先(Phase B)测试语义模型 ─────────────────────────
#
# 测试数据源(sqlite_registry/demo_registry)挂一个确定性语义模型:结构层
# (datasets/fields/relationships)由 schema 确定性生成 + 每个 dataset 带
# 「列名词表」描述(词重叠匹配,等价旧列名检索),metrics 覆盖 count + 数值
# 列聚合。产物是 kb init 能生成内容的子集,不违反 KB anti-cheating。

_NUMERIC_HINTS = ("int", "float", "double", "decimal", "numeric", "real")


def _test_terms(schema) -> list[dict]:
    """schema → flat term 列表(确定性):count + 数值列 SUM/AVG/MIN/MAX。"""
    terms: list[dict] = []
    for t in schema.tables:
        id_col = next(
            (c.name for c in t.columns
             if c.primary_key or c.name == "id" or c.name.endswith("_id")),
            None,
        )
        terms.append({
            "term": f"number of {t.name} records",
            "aliases": [f"{t.name} count", f"count of {t.name}"],
            "mapping": f"COUNT({t.name}.{id_col})" if id_col else "COUNT(*)",
            "tables": [t.name],
            "definition": f"total number of records in {t.name}",
        })
        for c in t.columns:
            if c.name == id_col:
                continue
            if any(m in str(c.type or "").lower() for m in _NUMERIC_HINTS):
                for fn in ("SUM", "AVG", "MIN", "MAX"):
                    terms.append({
                        "term": f"{fn.lower()} of {t.name}.{c.name}",
                        "aliases": [f"{fn.lower()} {c.name}"],
                        "mapping": f"{fn}({t.name}.{c.name})",
                        "tables": [t.name],
                        "definition": f"{fn.lower()} of {c.name} in {t.name}",
                    })
    return terms


async def make_test_semantic_provider(registry, kb_dir):
    """registry → 确定性语义模型 + provider(测试专用)。

    返回的 provider 挂到 registry._test_semantic_provider,graph 服务构造器
    默认注入(见各测试文件的 make_services)。无语义层的测试仍可显式传
    semantic_layer=None 走 no_model 拒绝路径。
    """
    import yaml

    from trove.services.kb.service import KbService
    from trove.services.kb.semantic_gen import generate_semantic_document
    from trove.services.semantic_layer.provider import SemanticLayerProvider

    ds = registry.default_name or "default"
    adapter = await registry.get(ds)
    schema = await adapter.get_schema()
    doc = generate_semantic_document(schema, model_name=ds, terms=_test_terms(schema))
    model_entry = doc["semantic_model"][0]
    # 测试夹具的中文别名(生产由 kb init 起草):让中文测试问题能锚定数据集
    _ZH_ALIASES = {
        "students": ["学生", "成绩"],
        "loan": ["贷款"],
        "account": ["账户"],
        "client": ["客户"],
        "district": ["地区", "区域"],
        "card": ["卡"],
        "disp": ["发放", "授权"],
        "order": ["订单"],
        "trans": ["交易"],
    }
    for d in model_entry.get("datasets", []):
        zh = _ZH_ALIASES.get(d["name"], [])
        d["ai_context"] = {"synonyms": [d["name"], *zh]}
        cols = ", ".join(f["name"] for f in d.get("fields", []))
        d["description"] = f"{d['name']} records; columns: {cols}"

    kb = KbService(kb_dir)
    kb.kb_dir.mkdir(parents=True, exist_ok=True)
    (kb.kb_dir / ds).mkdir(parents=True, exist_ok=True)
    (kb.semantics_path(ds)).write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    return SemanticLayerProvider(
        kb_dir / "semantic", ds,
        kb_semantics_path=kb.semantics_path(ds),
        dialect=adapter.dialect(),
    )


# ── Fixtures ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_trace_store(monkeypatch):
    """trace store 是进程级全局;每个测试从"未配置"状态开始,
    避免其它测试文件 configure_trace_store 的残留(如激活 RunTracer)。

    同时清除 LANGFUSE_* 环境(测试必须零 key/零网络;本机 shell 若导出过
    key 会意外启用 langfuse 客户端并尝试上报,拖慢/污染测试)。需要 Langfuse
    的测试自行用 monkeypatch.setenv 开启(见 tests/llm/test_observability.py)。
    """
    from trove.tracing.local import configure_trace_store
    configure_trace_store(None)
    from trove.llm.token_accounting import reset as reset_token_accounting
    reset_token_accounting()
    from trove.llm.token_calibration import reset as reset_token_calibration
    reset_token_calibration()
    from trove.llm import observability as _obs
    _obs._client = None
    for key in (
        "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
        "LANGFUSE_AUTH_CHECK",
    ):
        monkeypatch.delenv(key, raising=False)
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
async def sqlite_registry(tmp_path):
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

    # 语义优先(Phase B):给测试数据源挂一个确定性语义模型,
    # 供 graph 服务构造器默认注入(见 make_test_semantic_provider)。
    registry._test_semantic_provider = await make_test_semantic_provider(
        registry, tmp_path / "kb")

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

    registry._test_semantic_provider = await make_test_semantic_provider(
        registry, tmp_home / "kb")

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
        semantic_layer=getattr(sqlite_registry, "_test_semantic_provider", None),
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
