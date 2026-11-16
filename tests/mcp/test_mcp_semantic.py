"""MCP semantic context + deterministic query plane.

三条纪律:

1. **上下文是定向的**:``list_objects`` 给模型总览,``describe_object`` 给一个
   对象的字段/指标/关系 —— 不是把 semantics.yml 整份塞给 agent;
2. **查询走唯一执行路径**:``query_metrics`` 与 ``POST /v1/semantic/query``
   共用 ``services.semantic_query``(编译 → 授权/scope → 有界执行 → 脱敏);
3. **失败是结构化的**:编译错/未授权/超限都返回带 ``kind`` 的 dict,不抛栈,
   也绝不静默截断。
"""
from __future__ import annotations

import yaml

from tests.mcp.helpers import (
    FakeAuth as _FakeAuth,
    invoke as _invoke,
    server_with_identity as _server_with_identity,
)


async def test_list_objects_exposes_the_model_as_objects(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(server, "list_objects", datasource="test_db")

    assert payload["datasource"] == "test_db"
    assert payload["objects"]
    students = next(o for o in payload["objects"] if o["dataset"] == "students")
    # 生成器不声明 entity → declared=false;natural_keys 回退到 primary_key。
    assert students["declared"] is False
    assert students["natural_keys"] == ["id"]
    assert students["field_count"] == 4
    assert "number of students records" in students["metrics"]
    assert {m["name"] for m in payload["metrics"]} >= {
        "number of students records", "avg of students.grade",
    }
    assert "links" in payload and "hierarchies" in payload
    assert "topics" in payload


async def test_describe_object_by_alias(mcp_components):
    """对象名按数据集名/别名解析 —— 中文别名("学生")也直达。"""
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(
        server, "describe_object", object_name="学生", datasource="test_db",
    )

    assert payload["dataset"] == "students"
    fields = {f["name"] for f in payload["fields"]}
    assert {"id", "name", "grade", "county"} <= fields
    metric_names = {m["name"] for m in payload["metrics"]}
    assert "number of students records" in metric_names
    # 详情里的指标带表达式(总览只给摘要)
    metric = next(m for m in payload["metrics"]
                  if m["name"] == "number of students records")
    assert "COUNT" in metric["expression"]
    assert isinstance(payload["links"], list)


async def test_describe_object_not_found_lists_available(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(
        server, "describe_object", object_name="nope", datasource="test_db",
    )
    assert payload["kind"] == "object_not_found"
    assert "students" in payload["available"]


async def test_describe_object_requires_name(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(server, "describe_object", object_name="  ")
    assert payload["kind"] == "invalid_request"


async def test_query_metrics_matches_the_semantic_api(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(
        server, "query_metrics",
        metrics=["number of students records"],
        datasource="test_db",
        dimensions=["students.county"],
        order_by=[{"column": "students.county", "direction": "asc"}],
        limit=10,
    )

    assert payload["datasource"] == "test_db"
    assert payload["row_count"] == 3
    assert payload["columns"] == ["county", "number of students records"]
    assert payload["rows"] == [
        ["Alameda", 2], ["Los Angeles", 1], ["Orange", 2],
    ]
    assert "COUNT(students.id)" in payload["sql"]
    assert payload["data_scope"]["rewritten"] is False


async def test_query_metrics_without_limit_needs_no_order(mcp_components):
    """不传 limit 就不产生 LIMIT —— 编译器要求 LIMIT 必配 ORDER BY,
    给 MCP 加默认 limit 会让每个不带排序的查询编译失败。"""
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(
        server, "query_metrics",
        metrics=["number of students records"], datasource="test_db",
    )
    assert payload["row_count"] == 1
    assert "LIMIT" not in payload["sql"]


async def test_query_metrics_unknown_metric_is_a_compile_error(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(
        server, "query_metrics", metrics=["not_a_metric"], datasource="test_db",
    )
    assert payload["kind"] == "compile_error"
    assert "metric" in payload["error"].lower()


async def test_query_metrics_requires_metrics(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(server, "query_metrics", metrics=[], datasource="test_db")
    assert payload["kind"] == "invalid_request"


async def test_query_metrics_limit_is_bounded_not_truncated(mcp_components):
    """超上限报错,不静默截断 —— 截断会让 agent 把前 N 行当成完整答案。"""
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(
        server, "query_metrics", metrics=["number of students records"],
        datasource="test_db", limit=1001,
    )
    assert payload["kind"] == "invalid_request"
    assert "1000" in payload["error"]


async def test_query_metrics_masking_refusal_returns_no_rows(
    mcp_components, monkeypatch,
):
    """脱敏失败 = 拒绝:错误载荷里没有行(降级放行即泄漏)。"""
    from trove.services.authz.masking import MaskingError

    import trove.services.semantic_query as sq

    def _failing_masker(*, semantic_layer=None, config=None):
        def apply(rows, columns, **kwargs):
            raise MaskingError("salt 读不出来")
        return apply

    monkeypatch.setattr(sq, "build_masker", _failing_masker)
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(
        server, "query_metrics",
        metrics=["number of students records"], datasource="test_db",
    )
    assert payload["kind"] == "masking_refused", payload
    assert "rows" not in payload


async def test_no_semantic_model_is_an_explicit_kind(mcp_components):
    from trove.mcp.server import build_mcp_server

    # 删掉语义模型 → 工具必须说「没有模型」,而不是返回空对象清单。
    mcp_components["kb"].semantics_path("test_db").unlink()
    server = build_mcp_server(mcp_components)
    payload = await _invoke(server, "list_objects", datasource="test_db")
    assert payload["kind"] == "no_semantic_model"


async def test_semantic_tools_respect_datasource_grants(mcp_components):
    auth = _FakeAuth(
        grants_by_user={2: ["another_db"]},
        users={2: {"id": 2, "role": "user"}},
    )
    server = _server_with_identity(
        mcp_components, auth, {"id": 2, "role": "user"},
    )
    for tool, args in (
        ("list_objects", {}),
        ("describe_object", {"object_name": "students"}),
        ("query_metrics", {"metrics": ["number of students records"]}),
    ):
        payload = await _invoke(server, tool, datasource="test_db", **args)
        assert payload["kind"] == "not_allowed", (tool, payload)


async def test_list_objects_filters_topics_by_grants(mcp_components):
    """主题域按 ``topic_grants`` 过滤 —— 与 /semantic/topics 同一条面。"""
    kb = mcp_components["kb"]
    path = kb.semantics_path("test_db")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["topics"] = [
        {"name": "students_topic", "datasets": ["students"]},
        {"name": "secret_topic", "datasets": ["students"]},
    ]
    path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    auth = _FakeAuth(
        grants_by_user={2: ["test_db"]},
        users={2: {"id": 2, "role": "user"}},
        topic_grants_by_user={2: {"test_db": ["students_topic"]}},
    )
    server = _server_with_identity(
        mcp_components, auth, {"id": 2, "role": "user"},
    )

    overview = await _invoke(server, "list_objects", datasource="test_db")
    assert [t["name"] for t in overview["topics"]] == ["students_topic"]

    detail = await _invoke(
        server, "describe_object", object_name="students", datasource="test_db",
    )
    assert [t["name"] for t in detail["topics"]] == ["students_topic"]
