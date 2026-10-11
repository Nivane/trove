"""E2 写入面的 API 层:entity / relationship 草稿的收发一致。

三层各钉一段(互不重复):
- ``Literal`` 闸:非法 kind 在 Pydantic 层 422(kind 闭集在 schemas.py);
- 往返:POST drafts → 201 pending(模型不可见)→ confirm → semantics.yml
  与装载侧模型都看得见;
- validate 端点:新 kind 与前四类走同一条干跑路径(纯函数,坏草稿 200)。

写盘校验的细节(空声明/端点/键对/归属)在
``tests/services/semantic_layer/test_entity_relationship_drafts.py``;
这层只钉「端到端一次是真的一次」。
"""

from __future__ import annotations

import yaml


def _semantics_path(api_app):
    return api_app.state.kb.kb_dir / "test_db" / "semantics.yml"


def _model_doc(api_app) -> dict:
    return yaml.safe_load(_semantics_path(api_app).read_text(encoding="utf-8"))


async def _drafts(client) -> dict:
    resp = await client.get("/v1/admin/semantic/test_db")
    assert resp.status_code == 200, resp.text
    return resp.json()["semantic"]["drafts"]


async def test_unknown_kind_is_422(client, api_kb):
    """kind 是闭集:第五/六类之外的值在契约层就被挡下,不进业务代码。"""
    resp = await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "widget", "action": "upsert", "name": "x", "payload": {},
    })
    assert resp.status_code == 422


async def test_entity_draft_round_trip(client, api_app, api_kb):
    resp = await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "entity", "action": "upsert", "name": "students",
        "payload": {"dataset": "students", "display_name": "学生",
                    "natural_keys": ["grade"], "aliases": ["学生"]},
        "note": "会话里确认的对象命名",
    })
    assert resp.status_code == 201, resp.text
    draft = resp.json()["draft"]
    assert (draft["kind"], draft["name"], draft["status"]) == (
        "entity", "students", "pending")

    # pending 不碰单一真源
    before = _model_doc(api_app)
    students = next(d for d in before["semantic_model"][0]["datasets"]
                    if d["name"] == "students")
    assert "entity" not in students

    confirm = await client.post(
        f"/v1/admin/semantic/test_db/drafts/{draft['id']}/confirm")
    assert confirm.status_code == 200, confirm.text

    after = _model_doc(api_app)
    students = next(d for d in after["semantic_model"][0]["datasets"]
                    if d["name"] == "students")
    assert students["entity"] == {"display_name": "学生",
                                  "natural_keys": ["grade"],
                                  "aliases": ["学生"]}

    # 装载侧同一份字节:模型里读得到刚写的声明
    from trove.services.semantic_layer.ossie import parse_ossie
    model = parse_ossie(
        _semantics_path(api_app).read_text(encoding="utf-8"))
    ds = next(d for d in model.datasets if d.name == "students")
    assert ds.entity is not None and ds.entity.display_name == "学生"

    # 确认后草稿离开 pending(收件箱不再挂着它)
    drafts = await _drafts(client)
    assert [d["id"] for d in drafts["pending"]] == []


async def test_relationship_draft_round_trip(client, api_app, api_kb):
    resp = await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "relationship", "action": "upsert", "name": "county_grade",
        "payload": {"from": "students", "to": "students",
                    "from_columns": ["grade"], "to_columns": ["grade"],
                    "cardinality": "N:1", "display_name": "成绩归属"},
    })
    assert resp.status_code == 201, resp.text
    draft = resp.json()["draft"]

    confirm = await client.post(
        f"/v1/admin/semantic/test_db/drafts/{draft['id']}/confirm")
    assert confirm.status_code == 200, confirm.text

    rels = _model_doc(api_app)["semantic_model"][0]["relationships"]
    rel = next(r for r in rels if r["name"] == "county_grade")
    assert rel["from"] == "students" and rel["to"] == "students"
    assert rel["from_columns"] == ["grade"]
    assert rel["to_columns"] == ["grade"]
    assert rel["cardinality"] == "N:1"
    assert rel["display_name"] == "成绩归属"


async def test_entity_confirm_on_undeclared_dataset_is_400(client, api_kb):
    """创建不校验(草稿是提货单),确认才是落库门 —— 失败必须可读。"""
    draft = (await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "entity", "action": "upsert", "name": "ghost",
        "payload": {"dataset": "ghost", "display_name": "幽灵"},
    })).json()["draft"]
    resp = await client.post(
        f"/v1/admin/semantic/test_db/drafts/{draft['id']}/confirm")
    assert resp.status_code == 400
    assert "数据集未声明" in resp.json()["detail"]


async def test_validate_endpoint_accepts_new_kinds(client, api_kb):
    ok = await client.post("/v1/admin/semantic/test_db/validate", json={
        "kind": "entity", "action": "upsert", "name": "students",
        "payload": {"dataset": "students", "display_name": "学生"},
    })
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True and ok.json()["errors"] == []

    bad = await client.post("/v1/admin/semantic/test_db/validate", json={
        "kind": "relationship", "action": "upsert", "name": "ghost_rel",
        "payload": {"from": "students", "to": "ghost",
                    "from_columns": ["grade"], "to_columns": ["grade"],
                    "cardinality": "N:1"},
    })
    assert bad.status_code == 200, bad.text
    body = bad.json()
    assert body["ok"] is False
    messages = [i["message"] for i in body["errors"] + body["warnings"]]
    assert any("数据集未声明" in m for m in messages), messages
