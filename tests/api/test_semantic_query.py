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


MASKED_SEMANTICS = yaml.safe_dump({
    "version": "0.2.0.dev0",
    "semantic_model": [{
        "name": "test_db",
        "masking": {"default_policy": "apply"},
        "datasets": [{
            "name": "students",
            "source": "students",
            "primary_key": ["id"],
            "fields": [
                {"name": "id", "datatype": "Integer",
                 "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "id"}]}},
                {"name": "grade", "datatype": "Integer",
                 "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "grade"}]}},
                {"name": "county", "datatype": "String", "mask": "partial",
                 "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "county"}]}},
            ],
        }],
        "metrics": [{
            "name": "平均成绩",
            "description": "学生平均分",
            "expression": {
                "dialects": [{"dialect": "ANSI_SQL", "expression": "AVG(students.grade)"}],
            },
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


@pytest.fixture
async def masked_query_api(api_app):
    """Same app, but test_db's model declares ``mask: partial`` on county."""
    kb = api_app.state.kb
    ds_dir = kb.kb_dir / "test_db"
    ds_dir.mkdir(parents=True, exist_ok=True)
    (ds_dir / "semantics.yml").write_text(MASKED_SEMANTICS, encoding="utf-8")
    await kb.ensure_synced("test_db")
    return api_app


@pytest.mark.asyncio
async def test_semantic_query_masks_declared_field(masked_query_api, client):
    """声明的字段在出库前改写 —— 旁路 API 与图路径同一份规则。"""
    resp = await client.post("/v1/semantic/query", json={
        "datasource": "test_db",
        "metrics": ["平均成绩"],
        "dimensions": ["students.county"],
        "order_by": [{"column": "students.county", "direction": "asc"}],
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["masking_applied"]["fields"] == {"county": "partial"}
    counties = {r[0] for r in body["rows"]}
    assert counties == {"A*****a", "O****e", "Los****eles"}


@pytest.mark.asyncio
async def test_semantic_query_masking_refusal_returns_no_rows(
    masked_query_api, client, monkeypatch,
):
    """脱敏失败 = 拒绝这次查询:HTTP 错误,响应体里没有行(降级放行即泄漏)。"""
    from trove.services.authz.masking import MaskingError

    import trove.api.routers.semantic_query as sq

    def _failing_masker(*, semantic_layer=None, config=None):
        def apply(rows, columns, **kwargs):
            raise MaskingError("salt 读不出来")
        return apply

    monkeypatch.setattr(sq, "build_masker", _failing_masker)
    resp = await client.post("/v1/semantic/query", json={
        "datasource": "test_db",
        "metrics": ["平均成绩"],
        "dimensions": ["students.county"],
    })
    assert resp.status_code == 500
    assert "masking refused" in resp.json()["detail"]
    assert "rows" not in resp.json()


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
