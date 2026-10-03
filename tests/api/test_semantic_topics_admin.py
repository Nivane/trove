"""主题域的管理端审批流 + 用户端清单(HTTP 契约)。

三件事在这里守住:

* 主题域走**既有审批流**(``kind="topic"``):draft → confirm / reject,
  与 metric/field/dataset 同一套 pending 门禁、同一条写盘 lint 门禁 ——
  「新构造 = 新写入路径」是不被允许的(会绕开审核与 git 留痕);
* 两条**结构性硬校验**在写盘前拦下:``datasets`` 引用未声明的数据集
  (悬空作用域只会让边界静默变窄)、``datasets`` 为空(空边界不是
  "不限制",是把域内所有问题收敛成零锚定拒绝);
* 结构性问题在 detail/validate 里以 ``target.kind == "topic"`` 的条目形态
  出现 —— 前端据此定位到具体域,不必解析中文文案。

用户端 ``GET /v1/semantic/topics`` 是主题选择器的数据源:作用域给**生效
口径**,过期域**不隐藏**(``status="empty_scope"``)。
"""
from __future__ import annotations

import yaml


def _seed_topics(api_app, topics: list[dict]) -> None:
    """把主题域写进 api 夹具那份语义模型(provider 按 mtime 重读)。"""
    kb = api_app.state.kb
    path = kb.semantics_path("test_db")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["semantic_model"][0]["topics"] = topics
    path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


async def _create(client, payload: dict, action: str = "upsert",
                  name: str = "learners"):
    return await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "topic", "action": action, "name": name, "payload": payload,
    })


# ── 审批流:upsert / update(carryover)/ delete ──────────────


async def test_topic_upsert_draft_confirm(client, api_kb):
    resp = await _create(client, {
        "datasets": ["students"],
        "description": "学生域",
        "synonyms": ["学生主题"],
        "metrics": ["平均成绩"],
    })
    assert resp.status_code == 201, resp.text
    draft = resp.json()["draft"]
    assert draft["kind"] == "topic" and draft["status"] == "pending"

    # pending 不落盘;草稿卡片带着服务端算的 diff(before 不存在 → 新增)
    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    assert detail["model"]["topics"] == []
    pend = next(d for d in detail["drafts"]["pending"] if d["id"] == draft["id"])
    assert pend["diff"]["before"] is None
    assert pend["diff"]["after"]["datasets"] == ["students"]
    assert pend["diff"]["fields"][0]["after"] == "新增主题域"

    resp = await client.post(
        f"/v1/admin/semantic/test_db/drafts/{draft['id']}/confirm")
    assert resp.status_code == 200, resp.text
    assert resp.json()["draft"]["status"] == "applied"

    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    topic = next(t for t in detail["model"]["topics"] if t["name"] == "learners")
    assert topic["datasets"] == ["students"]
    assert topic["description"] == "学生域"
    assert topic["synonyms"] == ["学生主题"]
    assert topic["metrics"] == ["平均成绩"]


async def test_topic_update_carries_over_hand_written_fields(client, api_kb):
    """只改作用域的二次 upsert 不抹掉手写的 description/synonyms(carryover)。"""
    await _create(client, {"datasets": ["students"], "description": "学生域",
                           "synonyms": ["学生主题"]})
    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    first = next(d for d in detail["drafts"]["pending"] if d["name"] == "learners")
    await client.post(f"/v1/admin/semantic/test_db/drafts/{first['id']}/confirm")

    await _create(client, {"datasets": ["students"]})  # 仅重声明作用域
    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    second = next(d for d in detail["drafts"]["pending"] if d["name"] == "learners")
    await client.post(f"/v1/admin/semantic/test_db/drafts/{second['id']}/confirm")

    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    topic = next(t for t in detail["model"]["topics"] if t["name"] == "learners")
    assert topic["description"] == "学生域"
    assert topic["synonyms"] == ["学生主题"]


async def test_topic_delete_draft(client, api_kb):
    """topic 增删闭环:delete 草稿确认后模型里不再有该域。"""
    await _create(client, {"datasets": ["students"]})
    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    up = next(d for d in detail["drafts"]["pending"] if d["name"] == "learners")
    await client.post(f"/v1/admin/semantic/test_db/drafts/{up['id']}/confirm")

    resp = await _create(client, {}, action="delete")
    assert resp.status_code == 201, resp.text
    await client.post(
        f"/v1/admin/semantic/test_db/drafts/{resp.json()['draft']['id']}/confirm")

    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    assert all(t["name"] != "learners" for t in detail["model"]["topics"])


async def test_topic_reject_discards(client, api_kb):
    resp = await _create(client, {"datasets": ["students"]})
    draft_id = resp.json()["draft"]["id"]
    await client.post(f"/v1/admin/semantic/test_db/drafts/{draft_id}/reject")

    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    assert detail["model"]["topics"] == []
    assert any(d["status"] == "rejected" for d in detail["drafts"]["rejected"])


# ── 两条结构性硬校验(写盘前拦下)─────────────────────────


async def test_topic_undeclared_dataset_rejected_on_confirm(client, api_app, api_kb):
    path = api_app.state.kb.semantics_path("test_db")
    before = path.read_text(encoding="utf-8")
    resp = await _create(client, {"datasets": ["ghost_table"]})
    draft_id = resp.json()["draft"]["id"]

    resp = await client.post(
        f"/v1/admin/semantic/test_db/drafts/{draft_id}/confirm")
    assert resp.status_code == 400, resp.text
    assert "ghost_table" in resp.json()["detail"]
    assert path.read_text(encoding="utf-8") == before  # 拒绝 = 不落盘
    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    assert any(d["id"] == draft_id and d["status"] == "pending"
               for d in detail["drafts"]["pending"])


async def test_topic_empty_datasets_rejected_on_confirm(client, api_app, api_kb):
    """空边界不是"不限制":它会拒绝该域下的全部问题,必须挡在写盘前。"""
    resp = await _create(client, {"datasets": []})
    draft_id = resp.json()["draft"]["id"]
    resp = await client.post(
        f"/v1/admin/semantic/test_db/drafts/{draft_id}/confirm")
    assert resp.status_code == 400, resp.text
    assert "datasets 必填" in resp.json()["detail"]


# ── validate 干跑 + detail 的结构化问题条目 ─────────────────


async def test_validate_topic_dry_run(client, api_app, api_kb):
    path = api_app.state.kb.semantics_path("test_db")
    before = path.read_text(encoding="utf-8")

    resp = await client.post("/v1/admin/semantic/test_db/validate", json={
        "kind": "topic", "action": "upsert", "name": "learners",
        "payload": {"datasets": ["students"], "description": "学生域"},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["errors"] == [] and body["warnings"] == []
    assert body["normalized"]["expression"] == ""  # 主题域不是表达式草稿

    resp = await client.post("/v1/admin/semantic/test_db/validate", json={
        "kind": "topic", "action": "upsert", "name": "learners",
        "payload": {"datasets": ["ghost_table"]},
    })
    assert resp.status_code == 200, resp.text  # 校验器报告问题,自己不失败
    body = resp.json()
    assert body["ok"] is False
    err = body["errors"][0]
    assert set(err) == {"severity", "code", "target", "message", "hint"}
    assert err["severity"] == "error"
    assert err["code"] == "topic_undeclared_dataset"
    assert err["target"] == {"kind": "topic", "name": "learners"}
    assert "ghost_table" in err["message"] and err["hint"]
    # 纯函数:校验不改盘
    assert path.read_text(encoding="utf-8") == before


async def test_detail_reports_broken_topics_with_topic_target(client, api_app, api_kb):
    """坏主题域(悬空数据集 / 指标锚定越界)在 detail 里定位到具体域。"""
    kb = api_app.state.kb
    path = kb.semantics_path("test_db")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    model = doc["semantic_model"][0]
    # 一个与 平均成绩(锚定 students)无关的已声明数据集
    model["datasets"].append(
        {"name": "courses", "source": "courses", "primary_key": ["course_id"]})
    model["topics"] = [
        {"name": "ghosty", "datasets": ["ghost_table"]},
        {"name": "misplaced", "datasets": ["courses"], "metrics": ["平均成绩"]},
    ]
    path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    await kb.ensure_synced("test_db")

    resp = await client.get("/v1/admin/semantic/test_db")
    assert resp.status_code == 200, resp.text
    items = resp.json()["semantic"]["issue_items"]
    by_code: dict[str, list[dict]] = {}
    for it in items:
        by_code.setdefault(it["code"], []).append(it)
    assert by_code["topic_undeclared_dataset"][0]["target"] == {
        "kind": "topic", "name": "ghosty"}
    assert by_code["topic_metric_outside"][0]["target"] == {
        "kind": "topic", "name": "misplaced"}


# ── 用户端清单(选择器的数据源)───────────────────────────


async def test_topics_listing_for_granted_user(
    user_client, api_app, api_kb, auth_service,
):
    _seed_topics(api_app, [
        {"name": "learners", "description": "学生域", "datasets": ["students"]},
        {"name": "legacy", "datasets": ["dropped_table"]},  # 过期域
    ])
    await api_app.state.kb.ensure_synced("test_db")
    bob = await auth_service.authenticate("bob", "bobpw")
    await auth_service.set_datasources(bob["id"], ["test_db"])

    resp = await user_client.get(
        "/v1/semantic/topics", params={"datasource": "test_db"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["datasource"] == "test_db"
    by = {t["name"]: t for t in body["topics"]}
    assert set(by) == {"learners", "legacy"}  # 过期域不隐藏
    assert by["learners"]["scope"] == ["students"]
    assert by["learners"]["status"] == "ok"
    assert by["learners"]["description"] == "学生域"
    assert by["legacy"]["status"] == "empty_scope"
    assert by["legacy"]["scope"] == []


async def test_topics_listing_requires_grant(user_client, api_app, api_kb,
                                             auth_service):
    _seed_topics(api_app, [{"name": "learners", "datasets": ["students"]}])
    bob = await auth_service.authenticate("bob", "bobpw")
    await auth_service.set_datasources(bob["id"], ["other_db"])
    resp = await user_client.get(
        "/v1/semantic/topics", params={"datasource": "test_db"})
    assert resp.status_code == 403, resp.text


async def test_topics_listing_requires_auth(anon_client, api_kb):
    resp = await anon_client.get("/v1/semantic/topics")
    assert resp.status_code == 401


async def test_topics_listing_404_without_semantic_model(client):
    """无语义模型的数据源:整个问数面都不可用,清单同样显式 404。"""
    resp = await client.get(
        "/v1/semantic/topics", params={"datasource": "test_db"})
    assert resp.status_code == 404
