"""Admin semantic layer API tests — model detail + draft approval flow.

Covers the full semantic layer management surface: reading the OSSIE
model + lint issues, creating pending drafts, confirming (applies to
semantics.yml) and rejecting (discards) them, expression validation on
confirm, plus the usual auth/404 guards.
"""

from __future__ import annotations


async def test_semantic_detail(client, api_kb):
    """GET detail returns the parsed model, lint issues and an empty draft queue."""
    resp = await client.get("/v1/admin/semantic/test_db")
    assert resp.status_code == 200, resp.text
    sem = resp.json()["semantic"]
    assert sem["enabled"] is True
    model = sem["model"]
    assert model["metrics"][0]["name"] == "平均成绩"
    assert model["metrics"][0]["expression"] == "AVG(students.grade)"
    assert {d["name"] for d in model["datasets"]} == {"students"}
    assert isinstance(sem["issues"], list)
    assert sem["drafts"]["pending"] == []
    assert sem["drafts"]["applied"] == []
    assert sem["drafts"]["rejected"] == []


async def test_semantic_detail_unknown_ds_404(client):
    resp = await client.get("/v1/admin/semantic/nope")
    assert resp.status_code == 404


async def test_semantic_detail_user_forbidden(user_client, api_kb):
    resp = await user_client.get("/v1/admin/semantic/test_db")
    assert resp.status_code == 403


async def test_metric_upsert_draft_confirm(client, api_app, api_kb):
    """Create a pending metric draft → confirm applies it to semantics.yml."""
    resp = await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "metric",
        "action": "upsert",
        "name": "最高成绩",
        "payload": {
            "expression": "MAX(students.grade)",
            "synonyms": ["max grade"],
            "definition": "学生最高分",
            "datasets": ["students"],
        },
        "note": "业务新指标",
    })
    assert resp.status_code == 201, resp.text
    draft = resp.json()["draft"]
    assert draft["status"] == "pending"

    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    assert any(d["name"] == "最高成绩" for d in detail["drafts"]["pending"])
    # pending 不改 semantics.yml
    assert all(m["name"] != "最高成绩" for m in detail["model"]["metrics"])

    resp = await client.post(
        f"/v1/admin/semantic/test_db/drafts/{draft['id']}/confirm")
    assert resp.status_code == 200, resp.text
    assert resp.json()["draft"]["status"] == "applied"

    # 已落盘:parse 后 model 里有新 metric,且数据集声明保留
    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    names = [m["name"] for m in detail["model"]["metrics"]]
    assert "最高成绩" in names and "平均成绩" in names
    metric = next(m for m in detail["model"]["metrics"] if m["name"] == "最高成绩")
    assert metric["expression"] == "MAX(students.grade)"
    assert metric["synonyms"] == ["max grade"]
    assert metric["definition"] == "学生最高分"
    assert any(d["name"] == "students" for d in detail["model"]["datasets"])
    assert any(
        d["status"] == "applied" for d in detail["drafts"]["applied"])


async def test_metric_delete_draft(client, api_app, api_kb):
    """Delete draft removes the metric from semantics.yml on confirm."""
    resp = await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "metric", "action": "delete", "name": "平均成绩",
    })
    assert resp.status_code == 201
    draft_id = resp.json()["draft"]["id"]
    await client.post(f"/v1/admin/semantic/test_db/drafts/{draft_id}/confirm")

    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    assert all(m["name"] != "平均成绩" for m in detail["model"]["metrics"])


async def test_reject_draft_discards(client, api_app, api_kb):
    """Rejected drafts never touch semantics.yml."""
    resp = await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "metric", "action": "upsert",
        "name": "临时指标", "payload": {"expression": "COUNT(students.id)"},
    })
    draft_id = resp.json()["draft"]["id"]
    await client.post(f"/v1/admin/semantic/test_db/drafts/{draft_id}/reject")

    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    assert all(m["name"] != "临时指标" for m in detail["model"]["metrics"])
    assert any(
        d["status"] == "rejected" for d in detail["drafts"]["rejected"])


async def test_field_upsert_draft_confirm(client, api_app, api_kb):
    """Field draft targets dataset.field; confirm adds it to the dataset."""
    resp = await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "field", "action": "upsert",
        "name": "students.grade",
        "payload": {
            "expression": "grade",
            "datatype": "Integer",
            "semantic_role": "measure",
            "synonyms": ["score", "mark"],
            "description": "成绩",
            "is_time": False,
        },
        "note": "补充字段元数据",
    })
    assert resp.status_code == 201, resp.text
    draft_id = resp.json()["draft"]["id"]
    await client.post(f"/v1/admin/semantic/test_db/drafts/{draft_id}/confirm")

    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    ds = next(d for d in detail["model"]["datasets"] if d["name"] == "students")
    field = next(f for f in ds["fields"] if f["name"] == "grade")
    assert field["expression"] == "grade"
    assert field["datatype"] == "Integer"
    assert field["semantic_role"] == "measure"
    assert field["synonyms"] == ["score", "mark"]
    assert field["is_time"] is False


async def test_field_delete_draft(client, api_app, api_kb):
    """Field delete draft removes the field from the dataset."""
    # 先补一个字段再删,验证 field 增删闭环
    await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "field", "action": "upsert", "name": "students.grade",
        "payload": {"expression": "grade", "description": "成绩"},
    })
    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    pending = [d for d in detail["drafts"]["pending"] if d["name"] == "students.grade"]
    await client.post(f"/v1/admin/semantic/test_db/drafts/{pending[0]['id']}/confirm")

    await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "field", "action": "delete", "name": "students.grade",
    })
    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    delete_draft = next(d for d in detail["drafts"]["pending"] if d["action"] == "delete")
    await client.post(f"/v1/admin/semantic/test_db/drafts/{delete_draft['id']}/confirm")

    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    ds = next(d for d in detail["model"]["datasets"] if d["name"] == "students")
    assert all(f["name"] != "grade" for f in ds["fields"])


async def test_dataset_upsert_draft(client, api_app, api_kb):
    """Dataset draft creates a new declared dataset with source + pk."""
    resp = await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "dataset", "action": "upsert", "name": "courses",
        "payload": {
            "source": "courses",
            "primary_key": ["course_id"],
            "description": "课程表",
            "synonyms": ["class"],
        },
    })
    assert resp.status_code == 201, resp.text
    await client.post(f"/v1/admin/semantic/test_db/drafts/{resp.json()['draft']['id']}/confirm")

    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    ds = next(d for d in detail["model"]["datasets"] if d["name"] == "courses")
    assert ds["source"] == "courses"
    assert ds["primary_key"] == ["course_id"]
    assert ds["description"] == "课程表"


async def test_confirm_bad_expression_400(client, api_app, api_kb):
    """Unparseable expression is rejected on confirm (400), YAML untouched."""
    resp = await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "metric", "action": "upsert",
        "name": "坏指标", "payload": {"expression": "SELEC broken"},
    })
    draft_id = resp.json()["draft"]["id"]
    resp = await client.post(f"/v1/admin/semantic/test_db/drafts/{draft_id}/confirm")
    assert resp.status_code == 400
    assert "无法解析" in resp.json()["detail"]

    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    assert all(m["name"] != "坏指标" for m in detail["model"]["metrics"])
    assert any(d["status"] == "pending" for d in detail["drafts"]["pending"])


async def test_draft_unknown_404(client, api_app, api_kb):
    resp = await client.post("/v1/admin/semantic/test_db/drafts/nope/confirm")
    assert resp.status_code == 404
    assert (await client.post("/v1/admin/semantic/test_db/drafts/nope/reject")).status_code == 404


async def test_draft_reconfirm_400(client, api_app, api_kb):
    """已确认的草稿不能二次确认。"""
    resp = await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "metric", "action": "upsert",
        "name": "X", "payload": {"expression": "COUNT(1)"},
    })
    draft_id = resp.json()["draft"]["id"]
    await client.post(f"/v1/admin/semantic/test_db/drafts/{draft_id}/confirm")
    resp = await client.post(f"/v1/admin/semantic/test_db/drafts/{draft_id}/confirm")
    assert resp.status_code == 400


async def test_drafts_audited(client, api_app, api_kb):
    """Mutation writes audit entries (draft create/confirm)."""
    resp = await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "metric", "action": "upsert",
        "name": "审计指标", "payload": {"expression": "COUNT(1)"},
    })
    draft_id = resp.json()["draft"]["id"]
    await client.post(f"/v1/admin/semantic/test_db/drafts/{draft_id}/confirm")

    entries = await api_app.state.auth.list_audit(action="semantic.draft.create")
    assert any(e["username"] == "admin" and e["details"] == {
        "datasource": "test_db", "kind": "metric", "action": "upsert", "name": "审计指标",
    } for e in entries)
    confirms = await api_app.state.auth.list_audit(action="semantic.draft.confirm")
    assert any(e["username"] == "admin" and e["details"]["id"] == draft_id for e in confirms)
