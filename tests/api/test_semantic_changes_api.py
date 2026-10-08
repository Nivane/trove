"""变更评审 API —— 开单/列表/详情/验证/合并/驳回 + 409/422 契约 + 角色闸。"""
from __future__ import annotations


async def _open(client, payloads=None, note="测试变更"):
    resp = await client.post("/v1/admin/semantic/test_db/changes", json={
        "payloads": payloads or [{
            "kind": "metric", "action": "upsert", "name": "avg_grade",
            "payload": {"expression": "AVG(students.grade)"}}],
        "note": note,
    })
    return resp


async def test_open_list_detail_roundtrip(client):
    resp = await _open(client)
    assert resp.status_code == 201, resp.text
    change = resp.json()["change"]
    assert change["status"] == "open"
    assert change["subjects"] == [{"kind": "metric", "name": "avg_grade"}]

    listing = await client.get("/v1/admin/semantic/test_db/changes?status=open")
    assert listing.status_code == 200
    assert [c["id"] for c in listing.json()["changes"]] == [change["id"]]

    detail = await client.get(f"/v1/admin/semantic/test_db/changes/{change['id']}")
    assert detail.status_code == 200
    body = detail.json()["change"]
    assert body["diff"] is not None
    assert "metrics" in body["diff"]["entities"]
    assert "impact" in body and "verification" in body


async def test_verify_endpoint(client):
    change = (await _open(client)).json()["change"]
    resp = await client.post(
        f"/v1/admin/semantic/test_db/changes/{change['id']}/verify")
    assert resp.status_code == 200
    assert resp.json()["verification"]["verdict"] in (
        "neutral", "not_applicable", "improves", "unknown")


async def test_merge_then_stale_conflict(client):
    a = (await _open(client)).json()["change"]
    b = (await _open(client, [{"kind": "metric", "action": "upsert",
                               "name": "cnt", "payload": {"expression": "COUNT(students.grade)"}}])).json()["change"]
    ok = await client.post(f"/v1/admin/semantic/test_db/changes/{a['id']}/merge")
    assert ok.status_code == 200
    assert ok.json()["change"]["status"] == "merged"
    stale = await client.post(f"/v1/admin/semantic/test_db/changes/{b['id']}/merge")
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_change"


async def test_open_invalid_expression_returns_400(client):
    resp = await client.post("/v1/admin/semantic/test_db/changes", json={
        "payloads": [{"kind": "metric", "action": "upsert", "name": "bad",
                      "payload": {"expression": "SELEC nope("}}], "note": ""})
    assert resp.status_code == 400          # 开单干跑就拦下


async def test_merge_invalid_returns_422(client, api_app):
    """真 422:合并时的门禁不过 —— ``ChangeInvalid`` → ``change_invalid``。

    机制:``open`` 会干跑 payload,非法表达式在开单那一刻就是 400;要走到
    merge 的 422 分支,只能让记录里的 payload 与开单干跑时不同 —— 直接改
    落盘的 ``semantic_changes.yml``(``base_digest`` 记的是 semantics.yml 的
    文本,**不动它** → 乐观并发照过,冲突路径不抢先命中)。
    """
    import yaml

    change = (await _open(client)).json()["change"]
    path = api_app.state.kb.kb_dir / "test_db" / "semantic_changes.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["changes"][0]["payloads"][0]["payload"]["expression"] = "SELEC nope("
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8")

    resp = await client.post(
        f"/v1/admin/semantic/test_db/changes/{change['id']}/merge")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "change_invalid"


async def test_reject_requires_reason(client):
    change = (await _open(client)).json()["change"]
    empty = await client.post(
        f"/v1/admin/semantic/test_db/changes/{change['id']}/reject", json={"reason": ""})
    assert empty.status_code == 422          # pydantic min_length
    ok = await client.post(
        f"/v1/admin/semantic/test_db/changes/{change['id']}/reject",
        json={"reason": "口径不对"})
    assert ok.status_code == 200
    assert ok.json()["change"]["status"] == "rejected"


async def test_unknown_change_is_404(client):
    resp = await client.get("/v1/admin/semantic/test_db/changes/chg-nope")
    assert resp.status_code == 404


async def test_non_admin_cannot_open(user_client):
    resp = await user_client.post("/v1/admin/semantic/test_db/changes", json={
        "payloads": [{"kind": "metric", "action": "upsert", "name": "x",
                      "payload": {"expression": "AVG(students.grade)"}}], "note": ""})
    assert resp.status_code == 403
