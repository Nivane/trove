"""API test fixtures: FastAPI app over real services with a mock LLM.

Reuses the root conftest components: sqlite_registry (in-memory
students table) and session_manager (fully wired SessionManager over
the compiled reflection graph with a ScriptedGateway).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from trove.api.app import create_app
from trove.services.datasource.catalog import CatalogService
from trove.services.kb.service import KbService

from tests.helpers.kb import ossie_semantics_yaml

KB_SEED = {
    "semantics.yml": ossie_semantics_yaml([{
        "term": "平均成绩",
        "aliases": ["均分"],
        "mapping": "AVG(students.grade)",
        "tables": ["students"],
        "definition": "学生平均分",
    }]),
    "examples.yml": """examples:
  - question: 学生们的平均成绩是多少
    sql: SELECT county, AVG(grade) FROM students GROUP BY county
    tags: [成绩, 地区]
""",
    "lessons.yml": """lessons:
  - pattern: 日期列误当文本比较
    note: 先确认列类型再决定 strftime 还是直接比较
    sql_snippet: "WHERE year > '2020'"
    confirmed: false
""",
    "rules.yml": """rules:
  - rule: 金额单位统一为千元,展示前不做换算
""",
    "schema_notes.yml": """tables:
  - name: students
    description: 学生表
    columns:
      - name: grade
        description: 成绩
    metrics: []
""",
}


@pytest.fixture
async def api_app(sqlite_registry, session_manager, tmp_path):
    """FastAPI app with real catalog/registry/session-manager + empty KB."""
    kb = KbService(tmp_path / "proj")
    kb.kb_dir.mkdir(parents=True)
    app = create_app({
        "session_manager": session_manager,
        "catalog_service": CatalogService(sqlite_registry),
        "connector_registry": sqlite_registry,
        "kb": kb,
    })
    return app


@pytest.fixture
async def api_kb(api_app):
    """Seed the app's KB with one datasource's YAML files, synced to mirror."""
    kb = api_app.state.kb
    ds_dir = kb.kb_dir / "test_db"
    ds_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in KB_SEED.items():
        (ds_dir / filename).write_text(content, encoding="utf-8")
    await kb.ensure_synced("test_db")
    return api_app


@pytest.fixture
async def client(api_app):
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
