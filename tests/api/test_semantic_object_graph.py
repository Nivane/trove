"""E6 对象图端点 —— 语义模型的只读投影(设计 §4.5)。

``GET /v1/admin/semantic/{name}/object-graph``:形状快照(三个顶层键 +
逐项键集)、声明与回退在 HTTP 面上保真、fan_out 是布尔派生值、
错误形状两条(源未注册 / 无模型)、空模型是合法空图(N40)、
权限(非 admin 403 / 匿名 401)。
"""
from __future__ import annotations

import yaml

_OBJECT_KEYS = {"dataset", "display_name", "declared", "natural_keys",
                "aliases", "metrics", "field_count"}
_LINK_KEYS = {"name", "from", "to", "from_columns", "to_columns",
              "cardinality", "fan_out", "display_name"}
_HIERARCHY_KEYS = {"name", "display_name", "dataset", "levels"}


def _rich_model() -> dict:
    """带三块声明的最小富模型:entity / display_name / hierarchies 全在场。"""
    return {
        "version": "0.2.0.dev0",
        "semantic_model": [{
            "name": "test_db",
            "datasets": [
                {
                    "name": "students",
                    "source": "students",
                    "primary_key": ["id"],
                    "entity": {
                        "display_name": "学生",
                        "natural_keys": ["id"],
                        "aliases": ["学员"],
                    },
                    "fields": [{
                        "name": "grade", "datatype": "Integer",
                        "expression": {"dialects": [
                            {"dialect": "ANSI_SQL", "expression": "students.grade"}]},
                    }],
                },
                {"name": "courses", "source": "courses",
                 "primary_key": ["course_id"]},
            ],
            "relationships": [
                {
                    "name": "students_to_courses",
                    "from": "students",
                    "to": "courses",
                    "from_columns": ["id"],
                    "to_columns": ["course_id"],
                    "cardinality": "M:N",
                    "fan_out": "dedup",
                },
                {
                    "name": "courses_to_students",
                    "from": "courses",
                    "to": "students",
                    "from_columns": ["course_id"],
                    "to_columns": ["id"],
                    "cardinality": "N:M",
                    "display_name": "选课",
                },
            ],
            "metrics": [{
                "name": "平均成绩",
                "description": "学生平均分",
                "expression": {"dialects": [
                    {"dialect": "ANSI_SQL",
                     "expression": "AVG(students.grade)"}]},
            }],
            "hierarchies": [{
                "name": "grade_hierarchy",
                "display_name": "成绩层级",
                "dataset": "students",
                "levels": ["id", "grade"],
            }],
        }],
    }


def _seed(api_app, doc: dict) -> None:
    kb = api_app.state.kb
    path = kb.kb_dir / "test_db" / "semantics.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(
        doc, default_flow_style=False, allow_unicode=True, sort_keys=False,
    ), encoding="utf-8")


async def test_object_graph_dto_shape_snapshot(client, api_app, api_kb):
    """富模型 → DTO 三个顶层键 + 逐项键集钉死;声明与回退在 HTTP 面保真。

    端点直读 semantics.yml(不依赖 KB 镜像同步),所以这里改完文件直接问。
    """
    _seed(api_app, _rich_model())
    resp = await client.get("/v1/admin/semantic/test_db/object-graph")
    assert resp.status_code == 200, resp.text
    graph = resp.json()

    assert set(graph) == {"objects", "links", "hierarchies"}

    by_ds = {o["dataset"]: o for o in graph["objects"]}
    assert set(by_ds) == {"students", "courses"}
    assert set(by_ds["students"]) == _OBJECT_KEYS
    # 声明侧:entity 三键经投影透出;metrics 锚定到 students
    assert by_ds["students"]["declared"] is True
    assert by_ds["students"]["display_name"] == "学生"
    assert by_ds["students"]["natural_keys"] == ["id"]
    assert by_ds["students"]["aliases"] == ["学员"]
    assert by_ds["students"]["metrics"] == ["平均成绩"]
    assert by_ds["students"]["field_count"] == 1
    # 读侧回退:未声明 entity → name / primary_key / 无别名(设计 §4.1)
    assert by_ds["courses"]["declared"] is False
    assert by_ds["courses"]["display_name"] == "courses"
    assert by_ds["courses"]["natural_keys"] == ["course_id"]
    assert by_ds["courses"]["aliases"] == []
    assert by_ds["courses"]["metrics"] == []

    by_name = {l["name"]: l for l in graph["links"]}
    assert set(by_name) == {"students_to_courses", "courses_to_students"}
    for link in graph["links"]:
        assert set(link) == _LINK_KEYS
        assert isinstance(link["fan_out"], bool)
    # M:N + dedup → False(显式豁免);N:M 未豁免 → True
    assert by_name["students_to_courses"]["fan_out"] is False
    assert by_name["courses_to_students"]["fan_out"] is True
    # 原始豁免串不出面(cardinality 是声明的基数串,fan_out 只有布尔)
    assert by_name["students_to_courses"]["cardinality"] == "M:N"
    # display_name:声明优先;缺省回退列连接串(设计 §4.2)
    assert by_name["courses_to_students"]["display_name"] == "选课"
    assert by_name["students_to_courses"]["display_name"] == "id → course_id"

    assert graph["hierarchies"] == [{
        "name": "grade_hierarchy",
        "display_name": "成绩层级",
        "dataset": "students",
        "levels": ["id", "grade"],
    }]
    assert set(graph["hierarchies"][0]) == _HIERARCHY_KEYS


async def test_object_graph_default_seed_declares_nothing(client, api_kb):
    """默认 KB 种子(无任何新声明)→ 200,回退生效,links/hierarchies 空。"""
    resp = await client.get("/v1/admin/semantic/test_db/object-graph")
    assert resp.status_code == 200, resp.text
    graph = resp.json()
    assert set(graph) == {"objects", "links", "hierarchies"}
    assert [o["dataset"] for o in graph["objects"]] == ["students"]
    obj = graph["objects"][0]
    assert set(obj) == _OBJECT_KEYS
    assert obj["declared"] is False
    assert obj["display_name"] == "students"  # 缺省回退数据集 name
    assert obj["aliases"] == []
    assert graph["links"] == []
    assert graph["hierarchies"] == []


async def test_object_graph_unknown_datasource_404(client):
    resp = await client.get("/v1/admin/semantic/nope/object-graph")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "datasource not found"


async def test_object_graph_no_model_is_404(client, api_app):
    """源已注册但无 semantics.yml → 404(与 /v1/semantic/query 同一句错误)。"""
    resp = await client.get("/v1/admin/semantic/test_db/object-graph")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "no semantic model for datasource: test_db"


async def test_empty_model_is_empty_graph_not_404(client, api_app, api_kb):
    """N40:有文件、零数据集 = 合法空图(「读不到」与「没有东西」两回事)。"""
    _seed(api_app, {
        "version": "0.2.0.dev0",
        "semantic_model": [{"name": "test_db", "datasets": [], "metrics": []}],
    })
    resp = await client.get("/v1/admin/semantic/test_db/object-graph")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"objects": [], "links": [], "hierarchies": []}


async def test_object_graph_requires_admin(client, user_client, anon_client, api_kb):
    resp = await user_client.get("/v1/admin/semantic/test_db/object-graph")
    assert resp.status_code == 403
    resp = await anon_client.get("/v1/admin/semantic/test_db/object-graph")
    assert resp.status_code == 401
