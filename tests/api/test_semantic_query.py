"""POST /v1/semantic/query — standalone declarative semantic query API."""
import pytest
import yaml

SEMANTICS = yaml.safe_dump({
    "version": "0.1.0",
    "semantic_model": [{
        "name": "test_db",
        "datasets": [{
            "name": "students",
            "source": "students",
            "primary_key": ["id"],
            "fields": [
                {"name": "id", "datatype": "Integer",
                 "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "id"}]}},
                {"name": "grade", "datatype": "Integer",
                 "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "grade"}]}},
                {"name": "county", "datatype": "String",
                 "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "county"}]}},
            ],
        }],
        "metrics": [{
            "name": "平均成绩",
            "description": "学生平均分",
            "expression": {
                "dialects": [{"dialect": "ANSI_SQL", "expression": "AVG(students.grade)"}],
            },
            "ai_context": {"synonyms": ["均分"]},
        }],
    }],
}, default_flow_style=False, allow_unicode=True, sort_keys=False)


@pytest.fixture
async def query_api(api_app):
    """Seed the app's KB with a declared dataset + metric for test_db."""
    kb = api_app.state.kb
    ds_dir = kb.kb_dir / "test_db"
    ds_dir.mkdir(parents=True, exist_ok=True)
    (ds_dir / "semantics.yml").write_text(SEMANTICS, encoding="utf-8")
    await kb.ensure_synced("test_db")
    return api_app


@pytest.mark.asyncio
async def test_semantic_query_ok(query_api, client):
    resp = await client.post("/v1/semantic/query", json={
        "datasource": "test_db",
        "metrics": ["平均成绩"],
        "dimensions": ["students.county"],
        "order_by": [{"column": "students.county", "direction": "asc"}],
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "AVG(students.grade)" in body["sql"]
    assert "FROM students" in body["sql"]
    assert body["columns"] == ["county", "平均成绩"]
    assert body["row_count"] == 3
    counties = {r[0] for r in body["rows"]}
    assert counties == {"Alameda", "Orange", "Los Angeles"}


@pytest.mark.asyncio
async def test_semantic_query_unknown_metric(query_api, client):
    resp = await client.post("/v1/semantic/query", json={
        "datasource": "test_db",
        "metrics": ["not_a_metric"],
    })
    assert resp.status_code == 422
    assert "metric not declared" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_semantic_query_needs_metric(query_api, client):
    resp = await client.post("/v1/semantic/query", json={
        "datasource": "test_db",
        "metrics": [],
        "dimensions": ["students.county"],
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_semantic_query_requires_auth(query_api, anon_client):
    resp = await anon_client.post("/v1/semantic/query", json={
        "datasource": "test_db",
        "metrics": ["平均成绩"],
    })
    assert resp.status_code == 401
