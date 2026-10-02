"""POST /v1/admin/semantic/{ds}/preview —— 草稿试跑(语义工作台 P2 地基)。

契约:``{sql, columns, rows, row_count, masking_applied, warnings}`` ——
与 ``POST /v1/semantic/query`` 同形 + 草稿 lint 警告。三条不变量:

1. **零副作用**:试跑不改 semantics.yml / semantic_drafts.yml、不产生草稿;
2. **与查询入口同一套失败方向**:编译失败 422 / 超时 504 / 执行错 500 /
   脱敏拒绝 500 且响应体里没有行;
3. **warning 不拦试跑**(门禁拦 confirm,试跑零副作用),否则 warnings
   字段永远看不到值。
"""

from __future__ import annotations

import asyncio

import pytest

from tests.api.test_semantic_query import MASKED_SEMANTICS


async def _preview(client, body: dict, ds: str = "test_db"):
    return await client.post(f"/v1/admin/semantic/{ds}/preview", json=body)


@pytest.mark.asyncio
async def test_preview_metric_draft_defaults_metrics(client, api_kb):
    """metric 草稿可省略 query.metrics —— 默认试跑草稿自己。"""
    resp = await _preview(client, {
        "kind": "metric", "action": "upsert", "name": "最高成绩",
        "payload": {"expression": "MAX(students.grade)"},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"sql", "columns", "rows", "row_count",
                         "masking_applied", "warnings"}
    assert "MAX(students.grade)" in body["sql"]
    assert body["columns"] == ["最高成绩"]
    assert body["rows"] == [[99]]
    assert body["row_count"] == 1
    assert body["warnings"] == []
    # 未声明 mask 的文档:脱敏报告形状在,fields 为空
    assert body["masking_applied"]["fields"] == {}


@pytest.mark.asyncio
async def test_preview_explicit_query(client, api_kb):
    resp = await _preview(client, {
        "kind": "metric", "action": "upsert", "name": "最高成绩",
        "payload": {"expression": "MAX(students.grade)"},
        "query": {"metrics": ["平均成绩"]},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "AVG(students.grade)" in body["sql"]
    assert body["columns"] == ["平均成绩"]
    assert body["rows"][0][0] == pytest.approx(89.8)


@pytest.mark.asyncio
async def test_preview_warning_only_draft_still_runs(client, api_kb):
    """warning 级 lint(unique_keys 指向未声明列)拦 confirm,但不拦试跑 ——
    否则这个草稿永远走不到 preview,warnings 字段形同虚设。"""
    resp = await _preview(client, {
        "kind": "dataset", "action": "upsert", "name": "students",
        "payload": {"unique_keys": [["ghost_col"]]},
        "query": {"metrics": ["平均成绩"]},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [w["code"] for w in body["warnings"]] == ["unique_keys"]
    assert body["row_count"] == 1
    # 同一草稿 validate 会说不 ok(confirm 会被门禁拒) —— 两条口径不冲突
    check = await client.post("/v1/admin/semantic/test_db/validate", json={
        "kind": "dataset", "action": "upsert", "name": "students",
        "payload": {"unique_keys": [["ghost_col"]]},
    })
    assert check.json()["ok"] is False


@pytest.mark.asyncio
async def test_preview_bad_expression_422(client, api_kb):
    resp = await _preview(client, {
        "kind": "metric", "action": "upsert", "name": "坏指标",
        "payload": {"expression": "SUM(students.grade /"},
    })
    assert resp.status_code == 422
    assert "校验未通过" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_preview_query_unknown_metric_422(client, api_kb):
    resp = await _preview(client, {
        "kind": "metric", "action": "upsert", "name": "最高成绩",
        "payload": {"expression": "MAX(students.grade)"},
        "query": {"metrics": ["不存在"]},
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_preview_no_metrics_422(client, api_kb):
    """非 metric 草稿且没给 query.metrics → 明说需要指标,不猜。"""
    resp = await _preview(client, {
        "kind": "field", "action": "upsert", "name": "students.grade",
        "payload": {"expression": "grade"},
    })
    assert resp.status_code == 422
    assert "至少一个指标" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_preview_is_side_effect_free(client, api_app, api_kb):
    """试跑前后:semantics.yml / semantic_drafts.yml 字节不变、无新草稿。"""
    kb = api_app.state.kb
    paths = [kb.kb_dir / "test_db" / "semantics.yml",
             kb.kb_dir / "test_db" / "semantic_drafts.yml"]
    before = {p: p.read_text(encoding="utf-8") if p.exists() else None
              for p in paths}

    resp = await _preview(client, {
        "kind": "metric", "action": "upsert", "name": "最高成绩",
        "payload": {"expression": "MAX(students.grade)"},
    })
    assert resp.status_code == 200, resp.text

    after = {p: p.read_text(encoding="utf-8") if p.exists() else None
             for p in paths}
    assert after == before
    detail = (await client.get("/v1/admin/semantic/test_db")).json()["semantic"]
    assert detail["drafts"]["pending"] == []


@pytest.mark.asyncio
async def test_preview_applies_masking_from_draft_model(
    client, api_app, api_kb,
):
    """草稿改到脱敏字段时,试跑用**应用后的内存模型**判定 —— 读盘上旧模型
    会让试跑口径停在改动之前。这里直接验证已声明 mask 的字段被改写。"""
    kb = api_app.state.kb
    (kb.kb_dir / "test_db" / "semantics.yml").write_text(
        MASKED_SEMANTICS, encoding="utf-8")
    await kb.ensure_synced("test_db")

    resp = await _preview(client, {
        "kind": "metric", "action": "upsert", "name": "最高成绩",
        "payload": {"expression": "MAX(students.grade)"},
        "query": {"metrics": ["平均成绩"], "dimensions": ["students.county"]},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["masking_applied"]["fields"] == {"county": "partial"}
    counties = {r[0] for r in body["rows"]}
    assert counties == {"A*****a", "O****e", "Los****eles"}


@pytest.mark.asyncio
async def test_preview_masking_refusal_returns_no_rows(
    client, api_kb, monkeypatch,
):
    """脱敏失败 = 拒绝这次试跑:HTTP 错误 + 响应体里没有行(降级放行即泄漏)。"""
    from trove.services.authz.masking import MaskingError

    import trove.services.authz.masking as masking_mod

    def _failing_masker(*, semantic_layer=None, config=None):
        def apply(rows, columns, **kwargs):
            raise MaskingError("salt 读不出来")
        return apply

    monkeypatch.setattr(masking_mod, "build_masker", _failing_masker)
    resp = await _preview(client, {
        "kind": "metric", "action": "upsert", "name": "最高成绩",
        "payload": {"expression": "MAX(students.grade)"},
    })
    assert resp.status_code == 500
    assert "masking refused" in resp.json()["detail"]
    assert "rows" not in resp.json()


@pytest.mark.asyncio
async def test_preview_execution_timeout_504(
    client, api_kb, sqlite_registry, monkeypatch,
):
    import trove.api.routers.semantic as sem

    async def _slow_execute(sql, datasource=None):
        await asyncio.sleep(1)
        raise AssertionError("不应执行到这里")

    monkeypatch.setattr(sqlite_registry, "execute", _slow_execute)
    monkeypatch.setattr(sem, "_execute_timeout_s", lambda request: 0.01)
    resp = await _preview(client, {
        "kind": "metric", "action": "upsert", "name": "最高成绩",
        "payload": {"expression": "MAX(students.grade)"},
    })
    assert resp.status_code == 504
    assert "timed out" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_preview_unknown_datasource_404(client):
    resp = await _preview(client, {
        "kind": "metric", "action": "upsert", "name": "x",
        "payload": {"expression": "COUNT(1)"},
    }, ds="nope")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_preview_user_forbidden(user_client, api_kb):
    resp = await _preview(user_client, {
        "kind": "metric", "action": "upsert", "name": "x",
        "payload": {"expression": "COUNT(1)"},
    })
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_preview_requires_auth(anon_client, api_kb):
    resp = await _preview(anon_client, {
        "kind": "metric", "action": "upsert", "name": "x",
        "payload": {"expression": "COUNT(1)"},
    })
    assert resp.status_code == 401
