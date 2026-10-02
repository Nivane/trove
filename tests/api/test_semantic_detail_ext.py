"""GET /v1/admin/semantic/{ds} 扩展面 —— issue_items / draft.diff / drift 条目。

契约(方案「详情接口扩展」):

- ``issue_items[]`` = ``{severity, code, target:{kind,name}, message, hint}``
  (``issues`` 保留扁平旧形,两条内容同源);
- ``drafts.<status>[].diff`` = ``{kind,name,action,before,after,fields,error}``,
  ``fields[]`` = ``{f,before,after,changed}`` —— 服务端算,carryover 只在这里对;
- ``drift`` = ``{status, skip_reason, checked_at, items[]}``,条目带生命周期
  (``drift_id``/``first_seen_at``/``seen_count``)与影响面快照,与 drift store
  按 ``(level, subject)`` 对齐;catalog 不可达 → 显式 ``skipped``。
"""

from __future__ import annotations

import pytest
import yaml

DRIFTED_DOC = yaml.safe_dump({
    "version": "0.2.0.dev0",
    "semantic_model": [{
        "name": "test_db",
        "datasets": [
            {
                "name": "students",
                "fields": [
                    {"name": "grade", "datatype": "Integer",
                     "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "grade"}]}},
                    {"name": "ghost", "datatype": "String",
                     "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "ghost"}]}},
                ],
            },
            {"name": "courses", "source": "courses", "primary_key": ["course_id"]},
        ],
        "metrics": [{
            "name": "平均成绩",
            "description": "学生平均分",
            "expression": {
                "dialects": [{"dialect": "ANSI_SQL", "expression": "AVG(students.grade)"}],
            },
        }],
    }],
}, default_flow_style=False, allow_unicode=True, sort_keys=False)

DUP_METRIC_DOC = yaml.safe_dump({
    "version": "0.2.0.dev0",
    "semantic_model": [{
        "name": "test_db",
        "datasets": [{"name": "students"}],
        "metrics": [
            {"name": "平均成绩",
             "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                          "expression": "AVG(students.grade)"}]}},
            {"name": "平均成绩",
             "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                          "expression": "AVG(students.grade)"}]}},
        ],
    }],
}, default_flow_style=False, allow_unicode=True, sort_keys=False)


async def _seed(api_app, text: str):
    kb = api_app.state.kb
    (kb.kb_dir / "test_db" / "semantics.yml").write_text(text, encoding="utf-8")
    await kb.ensure_synced("test_db")


@pytest.mark.asyncio
async def test_detail_pending_draft_carries_diff(client, api_app, api_kb):
    resp = await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "metric", "action": "upsert", "name": "最高成绩",
        "payload": {"expression": "MAX(students.grade)"},
    })
    assert resp.status_code == 201, resp.text

    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    draft = next(d for d in detail["drafts"]["pending"] if d["name"] == "最高成绩")
    diff = draft["diff"]
    assert set(diff) == {"kind", "name", "action", "before", "after",
                         "fields", "error"}
    assert diff["kind"] == "metric" and diff["action"] == "upsert"
    assert diff["error"] is None
    assert diff["before"] is None
    assert diff["after"]["expression"] == "MAX(students.grade)"
    assert diff["fields"][0] == {"f": "动作", "before": "（不存在）",
                                 "after": "新增指标", "changed": True}
    assert all(set(row) == {"f", "before", "after", "changed"}
               for row in diff["fields"])


@pytest.mark.asyncio
async def test_detail_draft_diff_update_keeps_carryover(client, api_app, api_kb):
    """改表达式的 upsert:before 是盘上现值,carryover(同义词)不丢,
    而 description 不在 carryover 里 —— 更新即消失,但行上必须看得见。
    这套语义只有服务端知道,前端自己算 diff 必然漂移。"""
    await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "metric", "action": "upsert", "name": "平均成绩",
        "payload": {"expression": "MAX(students.grade)"},
    })
    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    draft = next(d for d in detail["drafts"]["pending"] if d["name"] == "平均成绩")
    diff = draft["diff"]
    assert diff["before"]["expression"] == "AVG(students.grade)"
    assert diff["after"]["expression"] == "MAX(students.grade)"
    rows = {r["f"]: r for r in diff["fields"]}
    assert rows["表达式 expression"]["changed"] is True
    # carryover 保住的键(ai_context 里的同义词)不显示为「被删除」
    assert diff["after"]["synonyms"] == ["均分"]
    assert rows["同义词 synonyms"]["changed"] is False
    # description 不在 carryover 里 → 更新即消失,行上看得见(不是悄悄删)
    assert rows["定义 description"]["before"] == "学生平均分"
    assert rows["定义 description"]["after"] == ""
    assert rows["定义 description"]["changed"] is True
    assert not any(r["f"] == "动作" for r in diff["fields"])


@pytest.mark.asyncio
async def test_detail_issue_items_structured(client, api_app, api_kb):
    """坏文档 → 扁平 issues 与结构化 issue_items 同源,目标/级别齐全。"""
    await _seed(api_app, DUP_METRIC_DOC)
    resp = await client.get("/v1/admin/semantic/test_db")
    assert resp.status_code == 200, resp.text
    sem = resp.json()["semantic"]
    assert any("重复定义" in s for s in sem["issues"])
    dup = next(i for i in sem["issue_items"] if i["code"] == "dup_metric")
    assert dup["severity"] == "error"
    assert dup["target"] == {"kind": "metric", "name": "平均成绩"}
    assert dup["hint"]


@pytest.mark.asyncio
async def test_detail_issue_items_empty_when_clean(client, api_kb):
    resp = await client.get("/v1/admin/semantic/test_db")
    assert resp.json()["semantic"]["issue_items"] == []


@pytest.mark.asyncio
async def test_detail_drift_lifecycle_from_store(client, api_app, api_kb):
    """实时漂移条目与 drift store 对齐:补上 first_seen_at/seen_count/
    drift_id 与影响面快照(L1 存储行不参与语义层页面)。"""
    from trove.services.drift import (
        L1,
        DriftItem,
        DriftReport,
        DriftStore,
        ImpactSet,
        from_semantic_drift,
    )

    await _seed(api_app, DRIFTED_DOC)

    root = api_app.state.kb.kb_dir.parent.parent
    store = DriftStore(root)
    try:
        report = from_semantic_drift({
            "status": "ok", "stale": True,
            "gone_tables": ["courses"],
            "missing_fields": {"students": ["ghost"]},
        }, "test_db")
        await store.record(report, impact=lambda item: ImpactSet(
            metrics=["平均成绩"] if item.subject == "courses" else []))
        # L1 结构层条目:语义层页面不该拿它冒充 L2 的生命周期
        await store.record(DriftReport(
            datasource="test_db", status="ok", generated_at="2026-01-01T00:00:00Z",
            items=[DriftItem(level=L1, kind="column_added", subject="students",
                             severity="info")],
        ))
    finally:
        await store.dispose()

    resp = await client.get("/v1/admin/semantic/test_db")
    assert resp.status_code == 200, resp.text
    drift = resp.json()["semantic"]["drift"]
    assert drift["status"] == "ok"
    by_subject = {i["subject"]: i for i in drift["items"]}
    assert set(by_subject) == {"courses", "students.ghost"}
    courses = by_subject["courses"]
    assert isinstance(courses["drift_id"], int)
    assert courses["first_seen_at"]
    assert courses["seen_count"] == 1
    assert courses["impact"] == {"metrics": ["平均成绩"], "examples": [],
                                 "rules": [], "lessons": []}
    assert courses["level"] == "L2"
    # L1 存储行没有把语义层条目串成生命周期
    assert by_subject["students.ghost"]["impact"]["metrics"] == []


@pytest.mark.asyncio
async def test_detail_drift_skipped_when_catalog_unreachable(
    client, api_app, api_kb, sqlite_registry, monkeypatch,
):
    """catalog 拿不到 → 显式 skipped(「没查成」不能显示成「没问题」)。"""

    async def _boom(name):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(sqlite_registry, "get", _boom)
    resp = await client.get("/v1/admin/semantic/test_db")
    assert resp.status_code == 200, resp.text
    drift = resp.json()["semantic"]["drift"]
    assert drift["status"] == "skipped"
    assert drift["skip_reason"] == "catalog_unreachable"
    assert drift["items"] == []
    assert drift["checked_at"]
