"""POST /v1/admin/semantic/{ds}/validate —— 草稿干跑校验(语义工作台 P2 地基)。

契约(形状钉死在 ``trove/api/schemas.py`` 的 response_model):
``{ok, errors[], warnings[], normalized:{expression}}``,
每条问题 ``{severity, code, target:{kind,name}, message, hint}``。

这个端点是**纯函数**:坏草稿也回 200(它报告问题,自己不是问题的来源)——
所以「不写盘」和「字段形状」都要钉。
"""

from __future__ import annotations

import pytest


def _assert_issue_shape(item: dict) -> None:
    assert set(item) == {"severity", "code", "target", "message", "hint"}
    assert item["severity"] in ("error", "warning")
    assert item["code"]
    assert set(item["target"]) == {"kind", "name"}
    assert item["message"]
    assert item["hint"]


@pytest.mark.asyncio
async def test_validate_clean_draft_ok(client, api_kb):
    resp = await client.post("/v1/admin/semantic/test_db/validate", json={
        "kind": "metric", "action": "upsert", "name": "学生人数",
        "payload": {"expression": "COUNT(students.grade)",
                    "datasets": ["students"]},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"ok", "errors", "warnings", "normalized"}
    assert body["ok"] is True
    assert body["errors"] == [] and body["warnings"] == []
    assert body["normalized"]["expression"] == "COUNT(students.grade)"


@pytest.mark.asyncio
async def test_validate_reports_issues_without_400(client, api_app, api_kb):
    """坏草稿 → 200 + 结构化 errors(不是 4xx:校验器报告问题,自己不失败)。"""
    path = api_app.state.kb.semantics_path("test_db")
    before = path.read_text(encoding="utf-8")
    resp = await client.post("/v1/admin/semantic/test_db/validate", json={
        "kind": "metric", "action": "upsert", "name": "坏指标",
        "payload": {"expression": "SUM(grade /"},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    _assert_issue_shape(body["errors"][0])
    assert body["errors"][0]["code"] == "expr_parse"
    assert body["errors"][0]["target"]["kind"] == "metric"
    assert body["normalized"]["expression"] == ""
    # 纯函数:校验不改盘
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_validate_undeclared_anchor_is_error(client, api_kb):
    """锚定未声明的数据集 —— 静默补建空壳已改为显式报错(方案点名的缺陷)。"""
    resp = await client.post("/v1/admin/semantic/test_db/validate", json={
        "kind": "metric", "action": "upsert", "name": "贷款均值",
        "payload": {"expression": "AVG(loan.amount)", "datasets": ["loan"]},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    err = body["errors"][0]
    assert err["code"] == "dataset_undeclared"
    assert err["target"] == {"kind": "metric", "name": "贷款均值"}
    assert "loan" in err["message"]


@pytest.mark.asyncio
async def test_validate_field_draft_contract(client, api_kb):
    resp = await client.post("/v1/admin/semantic/test_db/validate", json={
        "kind": "field", "action": "upsert", "name": "students.grade",
        "payload": {"expression": "grade", "datatype": "Integer"},
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert resp.json()["normalized"]["expression"] == "grade"


@pytest.mark.asyncio
async def test_validate_delete_draft_ok(client, api_kb):
    resp = await client.post("/v1/admin/semantic/test_db/validate", json={
        "kind": "metric", "action": "delete", "name": "平均成绩",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
async def test_validate_rejects_bad_body(client, api_kb):
    """payload 形状由 pydantic 拦(422),不进纯函数。"""
    resp = await client.post("/v1/admin/semantic/test_db/validate", json={
        "kind": "column", "action": "upsert", "name": "x",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_validate_unknown_datasource_404(client):
    resp = await client.post("/v1/admin/semantic/nope/validate", json={
        "kind": "metric", "action": "upsert", "name": "x",
        "payload": {"expression": "COUNT(students.grade)"},
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_validate_user_forbidden(user_client, api_kb):
    resp = await user_client.post("/v1/admin/semantic/test_db/validate", json={
        "kind": "metric", "action": "upsert", "name": "x",
        "payload": {"expression": "COUNT(students.grade)"},
    })
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_validate_requires_auth(anon_client, api_kb):
    resp = await anon_client.post("/v1/admin/semantic/test_db/validate", json={
        "kind": "metric", "action": "upsert", "name": "x",
        "payload": {"expression": "COUNT(students.grade)"},
    })
    assert resp.status_code == 401
