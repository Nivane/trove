"""漂移检查端点的级别契约 —— 默认 L1+L2、L3 显式请求、L4 明确拒绝。

这条接线最容易出的错是「请求了 L3 却跑了默认」:响应看起来正常,但
``levels_verified`` 里没有 L3 —— 调用方若只看 200,会把没查当成查过。
所以这里从 HTTP 面钉住三件事:

1. 不带 ``level`` → L3 不进 ``levels_verified``、载荷无 ``unverified``;
2. ``?level=L3`` → 真的对活库探测(声明值缺失成为 L3 条目);
3. ``?level=L4`` → 400 —— L4 只承接外部声明,永不自动检测。
"""

from __future__ import annotations

import pytest

#: 一个最小的 OSSIE 0.2 语义模型:声明了 students.county 的两个取值。
#: 活库是三行 ``Alameda / Orange / Los Angeles`` —— 前两个必须仍在。
_SEMANTICS = """version: "0.2.0.dev0"
name: drift_api_test
datasets:
  - name: students
    source: students
    fields:
      - name: county
        datatype: String
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: county
        values:
{values}
"""


def _write_semantics(api_app, values=("Alameda", "Orange")) -> None:
    kb = api_app.state.kb
    ds_dir = kb.kb_dir / "test_db"
    ds_dir.mkdir(parents=True, exist_ok=True)
    vals = "".join(f"          - '{v}'\n" for v in values)
    (ds_dir / "semantics.yml").write_text(
        _SEMANTICS.format(values=vals), encoding="utf-8")


async def test_default_check_does_not_run_l3(client, api_kb):
    """默认检测与 L3 接入前一致:不探测活库、载荷无 unverified 键。"""
    resp = await client.post("/v1/admin/drift/check?datasource=test_db")

    assert resp.status_code == 200
    body = resp.json()
    assert "L3" not in body["levels_verified"]
    assert "unverified" not in body


async def test_l3_is_opt_in_and_verified_when_everything_is_judged(
        client, api_app, api_kb):
    _write_semantics(api_app)

    resp = await client.post(
        "/v1/admin/drift/check?datasource=test_db&level=L3")

    assert resp.status_code == 200
    body = resp.json()
    assert body["levels_verified"] == ["L3"]
    assert body["items"] == []
    assert "unverified" not in body


async def test_l3_reports_a_declared_value_that_left_the_data(
        client, api_app, api_kb):
    """声明值消失 → warning 条目;这不是「查不成」,仍 200。"""
    _write_semantics(api_app, values=("Alameda", "Nowhere"))

    resp = await client.post(
        "/v1/admin/drift/check?datasource=test_db&level=L3")

    assert resp.status_code == 200
    body = resp.json()
    items = [i for i in body["items"] if i["kind"] == "value_missing"]
    assert len(items) == 1
    assert items[0]["level"] == "L3"
    assert items[0]["severity"] == "warning"
    assert items[0]["detail"]["missing"] == ["Nowhere"]
    assert body["levels_verified"] == ["L3"]


async def test_deep_check_without_semantics_model_still_answers(
        client, api_app, api_kb):
    """没有 semantics.yml = 没有可违反的契约,**不适用**,不是失败。

    管理台深度检测发的是 L1+L2+L3;一个从没建过语义模型的库不该因此
    整份 503 —— L1 的结论照常交付,L3 只是不在 ``levels_verified`` 里。
    """
    kb = api_app.state.kb
    (kb.kb_dir / "test_db" / "semantics.yml").unlink(missing_ok=True)

    resp = await client.post(
        "/v1/admin/drift/check?datasource=test_db"
        "&level=L1&level=L2&level=L3")

    assert resp.status_code == 200
    body = resp.json()
    assert body["levels_verified"] == ["L1"]
    assert "unverified" not in body


async def test_l3_only_without_semantics_model_is_skipped_not_clean(
        client, api_app):
    """只请求 L3 而该库没有语义层:一路上都没跑成 → 未完成,不是「干净」。"""
    kb = api_app.state.kb
    (kb.kb_dir / "test_db" / "semantics.yml").unlink(missing_ok=True)

    resp = await client.post(
        "/v1/admin/drift/check?datasource=test_db&level=L3")

    assert resp.status_code == 503
    assert resp.json()["detail"]["skip_reason"] == "no_detector_ran"


async def test_l4_is_rejected_with_400(client, api_kb):
    resp = await client.post(
        "/v1/admin/drift/check?datasource=test_db&level=L4")

    assert resp.status_code == 400
    assert "L4" in resp.json()["detail"]


@pytest.mark.parametrize("level", ["L1", "L2"])
async def test_explicit_l1_l2_still_work(client, api_kb, level):
    resp = await client.post(
        f"/v1/admin/drift/check?datasource=test_db&level={level}")

    assert resp.status_code == 200
    assert resp.json()["levels_verified"] == [level]
