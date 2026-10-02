"""POST /v1/admin/semantic/{ds}/drafts/batch —— 批量审批(语义工作台 P2 地基)。

契约:``{results: [{id, ok, error}], applied, failed}`` —— **逐条独立**
(一条失败不影响其余),失败原因逐条显式给出,且逐条审计(details.batch=True,
成功条目带 kind/name,失败条目带 error)。批量确认写的 git trailer 是
``Generator: semantic.batch``(与逐条点击可区分,见 test_git_versioning)。
"""

from __future__ import annotations

import pytest


async def _make_draft(client, name: str, expression: str, **extra) -> str:
    resp = await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "metric", "action": "upsert", "name": name,
        "payload": {"expression": expression, "datasets": ["students"], **extra},
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["draft"]["id"]


@pytest.mark.asyncio
async def test_batch_confirm_mixed_results(client, api_app, api_kb):
    """一条能过、一条坏、一条幽灵 id —— 各自成败显式,互不影响。"""
    good = await _make_draft(client, "最高成绩", "MAX(students.grade)")
    bad = await _make_draft(client, "坏指标", "SELEC broken")

    resp = await client.post("/v1/admin/semantic/test_db/drafts/batch", json={
        "ids": [good, "ghost-id", bad], "action": "confirm",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"results", "applied", "failed"}
    assert body["applied"] == 1 and body["failed"] == 2
    by_id = {r["id"]: r for r in body["results"]}
    assert set(by_id) == {good, "ghost-id", bad}
    assert by_id[good] == {"id": good, "ok": True, "error": None}
    assert by_id["ghost-id"]["ok"] is False and by_id["ghost-id"]["error"]
    assert by_id[bad]["ok"] is False and "草稿" in by_id[bad]["error"]

    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    metrics = [m["name"] for m in detail["model"]["metrics"]]
    assert "最高成绩" in metrics and "坏指标" not in metrics
    # 成功的 applied;坏的留在 pending(不是「不知道跑没跑」)
    assert any(d["name"] == "最高成绩" and d["status"] == "applied"
               for d in detail["drafts"]["applied"])
    assert any(d["name"] == "坏指标" and d["status"] == "pending"
               for d in detail["drafts"]["pending"])


@pytest.mark.asyncio
async def test_batch_reject_discards_without_touching_yaml(
        client, api_app, api_kb):
    a = await _make_draft(client, "甲", "COUNT(students.id)")
    b = await _make_draft(client, "乙", "COUNT(students.grade)")
    path = api_app.state.kb.kb_dir / "test_db" / "semantics.yml"
    before = path.read_text(encoding="utf-8")

    resp = await client.post("/v1/admin/semantic/test_db/drafts/batch", json={
        "ids": [a, b], "action": "reject",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "results": [{"id": a, "ok": True, "error": None},
                    {"id": b, "ok": True, "error": None}],
        "applied": 2, "failed": 0,
    }
    assert path.read_text(encoding="utf-8") == before
    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    assert {d["name"] for d in detail["drafts"]["rejected"]} == {"甲", "乙"}


@pytest.mark.asyncio
async def test_batch_audits_each_item_with_batch_flag(client, api_app, api_kb):
    good = await _make_draft(client, "最高成绩", "MAX(students.grade)")
    bad = await _make_draft(client, "坏指标", "SELEC broken")
    await client.post("/v1/admin/semantic/test_db/drafts/batch", json={
        "ids": [good, bad], "action": "confirm", "note": "批量过一遍",
    })

    entries = await api_app.state.auth.list_audit(action="semantic.draft.confirm")
    batch = [e for e in entries if (e["details"] or {}).get("batch")]
    assert {e["details"]["id"] for e in batch} == {good, bad}
    ok = next(e for e in batch if e["details"]["id"] == good)
    assert ok["username"] == "admin"
    assert ok["status"] == 200
    assert ok["details"]["kind"] == "metric"
    assert ok["details"]["name"] == "最高成绩"
    assert ok["details"]["note"] == "批量过一遍"
    fail = next(e for e in batch if e["details"]["id"] == bad)
    assert fail["status"] == 400 and fail["details"]["error"]


@pytest.mark.asyncio
async def test_batch_reject_audited(client, api_app, api_kb):
    a = await _make_draft(client, "甲", "COUNT(students.id)")
    await client.post("/v1/admin/semantic/test_db/drafts/batch", json={
        "ids": [a], "action": "reject",
    })
    entries = await api_app.state.auth.list_audit(action="semantic.draft.reject")
    assert any((e["details"] or {}).get("batch") and e["details"]["id"] == a
               for e in entries)


@pytest.mark.asyncio
async def test_batch_empty_ids_422(client, api_kb):
    resp = await client.post("/v1/admin/semantic/test_db/drafts/batch", json={
        "ids": [], "action": "confirm",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_batch_bad_action_422(client, api_app, api_kb):
    a = await _make_draft(client, "甲", "COUNT(students.id)")
    resp = await client.post("/v1/admin/semantic/test_db/drafts/batch", json={
        "ids": [a], "action": "apply",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_batch_unknown_datasource_404(client):
    resp = await client.post("/v1/admin/semantic/nope/drafts/batch", json={
        "ids": ["x"], "action": "confirm",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_batch_user_forbidden(user_client, api_kb):
    resp = await user_client.post("/v1/admin/semantic/test_db/drafts/batch", json={
        "ids": ["x"], "action": "confirm",
    })
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_batch_requires_auth(anon_client, api_kb):
    resp = await anon_client.post("/v1/admin/semantic/test_db/drafts/batch", json={
        "ids": ["x"], "action": "confirm",
    })
    assert resp.status_code == 401
