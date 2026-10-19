"""Evalset admin API tests (real services on tmp dirs, zero LLM/network).

Covers the HTTP surface over Tasks 7–9: the confirmation gate, readiness,
run/generate background tasks, preview masking and the ``latest_verdict``
embedding (including its "unreadable is not zero" degraded state).

``test_db`` is the datasource registered by root conftest's
``sqlite_registry`` — ``_ds_id_or_404`` resolves names against the live
registry, so the tests use that name rather than a synthetic one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from trove.api.app import create_app
from trove.core.config import AgentConfig
from trove.llm.gateway import LLMGateway
from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.config_store import ConfigStore
from trove.services.kb.init_tasks import init_tasks
from trove.services.kb.service import KbService

DS = "test_db"
BASE = f"/v1/admin/datasources/{DS}/evalset"


# ── fixtures ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_init_tasks():
    """R7: ``init_tasks`` 是进程级单例,任务不得跨测试泄漏。"""
    init_tasks.reset()
    yield
    init_tasks.reset()


@pytest.fixture
async def evalset_app(sqlite_registry, tmp_path, auth_service):
    kb = KbService(tmp_path / "proj")
    kb.kb_dir.mkdir(parents=True)
    await kb.ensure_synced(None)
    app = create_app({
        "session_manager": None,
        "catalog_service": CatalogService(sqlite_registry),
        "connector_registry": sqlite_registry,
        "kb": kb,
        "auth": auth_service,
        "config_store": ConfigStore(tmp_path / "proj" / ".trove" / "datasources.yml"),
        "llm_gateway": LLMGateway(mock_response="x"),
        "config": AgentConfig(target="mock/model"),
    })
    yield app


@pytest.fixture
async def admin_client(evalset_app, admin_token):
    transport = ASGITransport(app=evalset_app)
    headers = {"Authorization": f"Bearer {admin_token}"}
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers=headers) as c:
        yield c


@pytest.fixture
async def noauth_client(evalset_app):
    transport = ASGITransport(app=evalset_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def plain_user_client(evalset_app, user_token):
    transport = ASGITransport(app=evalset_app)
    headers = {"Authorization": f"Bearer {user_token}"}
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers=headers) as c:
        yield c


# ── helpers ──────────────────────────────────────────────


def _project_root(app) -> Path:
    return Path(app.state.kb.kb_dir).parent.parent


async def _create_item(client, *, question="有多少学生？",
                       sql="SELECT COUNT(*) FROM students", **extra):
    r = await client.post(f"{BASE}/items",
                          json={"question": question, "reference_sql": sql,
                                **extra})
    assert r.status_code == 200, r.text
    return r.json()


async def _wait_status(client, url: str, timeout: float = 5.0) -> dict:
    """轮询任务状态到终态(done/error),超时即失败。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    body: dict = {}
    while loop.time() < deadline:
        r = await client.get(url)
        assert r.status_code == 200, r.text
        body = r.json()
        if body.get("status") in ("done", "error"):
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"task did not finish in time: {body}")


def _write_summary(app, tier: str, name: str, payload: dict) -> Path:
    run_dir = (
        _project_root(app) / ".trove" / "eval" / "evalsets" / DS / tier
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ── auth / list ──────────────────────────────────────────


class TestAuth:
    async def test_list_requires_token(self, noauth_client):
        r = await noauth_client.get(BASE)
        assert r.status_code == 401, r.text

    async def test_list_requires_admin(self, plain_user_client):
        r = await plain_user_client.get(BASE)
        assert r.status_code == 403, r.text


class TestItems:
    async def test_create_lands_pending_and_reports_tier(self, admin_client):
        body = await _create_item(admin_client)
        assert body["status"] == "pending"
        assert body["tier"] == "verified"
        assert body["source"] == "manual"
        assert body["evalset_rev"]

    async def test_patch_edits_a_pending_item(self, admin_client):
        item = await _create_item(admin_client)
        r = await admin_client.patch(
            f"{BASE}/{item['qid']}", json={"question": "学生总人数是多少？"})
        assert r.status_code == 200, r.text
        assert r.json()["question"] == "学生总人数是多少？"

    async def test_confirm_batch_applies(self, admin_client):
        first = await _create_item(
            admin_client, question="有多少学生？",
            sql="SELECT COUNT(*) FROM students")
        second = await _create_item(
            admin_client, question="平均分是多少？",
            sql="SELECT AVG(grade) FROM students")
        r = await admin_client.post(
            f"{BASE}/confirm", json={"qids": [first["qid"], second["qid"]]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] == 2
        assert body["failed"] == 0

    async def test_confirm_unknown_qid_is_404(self, admin_client):
        r = await admin_client.post(f"{BASE}/e-doesnotexist/confirm")
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == "evalset.not_found"


# ── readiness / runs ─────────────────────────────────────


class TestReadiness:
    async def test_readiness_is_not_swallowed_by_the_qid_route(self, admin_client):
        r = await admin_client.get(f"{BASE}/readiness")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["datasource"] == DS
        # 三态:没有 run 文件 = null(不是 0)。
        assert body["latest_runs"] == {
            "verified": None, "semantic": None, "synthetic": None}
        assert body["available"] is True


class TestRuns:
    async def test_missing_run_directory_is_an_empty_list(self, admin_client):
        r = await admin_client.get(f"{BASE}/runs")
        assert r.status_code == 200, r.text
        assert r.json() == {"datasource": DS, "runs": [], "degraded": []}

    async def test_renders_summaries_and_skips_latest(self, admin_client,
                                                      evalset_app):
        _write_summary(evalset_app, "verified", "evalset-x.summary.json", {
            "run_id": "evalset-x", "tier": "verified",
            "at": "2026-10-18T00:00:00+00:00", "n": 2,
        })
        # latest.summary.json 是原子副本,不是一次 run(R17)。
        _write_summary(evalset_app, "verified", "latest.summary.json", {
            "run_id": "latest", "tier": "verified",
            "at": "2026-10-20T00:00:00+00:00", "n": 9,
        })
        _write_summary(evalset_app, "semantic", "evalset-y.summary.json", {
            "run_id": "evalset-y", "tier": "semantic",
            "at": "2026-10-19T00:00:00+00:00", "n": 1,
        })
        # 坏文件:跳过并记一条 degraded,不让整个列表 500。
        _write_summary(evalset_app, "verified", "bad.summary.json", "{bad")

        r = await admin_client.get(f"{BASE}/runs")
        assert r.status_code == 200, r.text
        body = r.json()
        assert [run["run_id"] for run in body["runs"]] == ["evalset-y", "evalset-x"]
        assert body["runs"][0]["tier"] == "semantic"
        assert all(run["run_id"] != "latest" for run in body["runs"])
        assert any(d["code"] == "run_store_unreadable"
                   for d in body["degraded"])


class TestLatestVerdict:
    async def test_no_run_file_means_null_not_zero(self, admin_client):
        item = await _create_item(admin_client)
        r = await admin_client.get(BASE)
        assert r.status_code == 200, r.text
        body = r.json()
        row = next(x for x in body["items"] if x["qid"] == item["qid"])
        assert "latest_verdict" in row
        assert row["latest_verdict"] is None
        assert body["degraded"] == []

    async def test_corrupt_latest_entries_degrade_to_null(self, admin_client,
                                                          evalset_app):
        item = await _create_item(admin_client)
        run_dir = (
            _project_root(evalset_app) / ".trove" / "eval" / "evalsets"
            / DS / "verified"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "latest.jsonl").write_text("{not json\n", encoding="utf-8")

        r = await admin_client.get(BASE)
        assert r.status_code == 200, r.text
        body = r.json()
        row = next(x for x in body["items"] if x["qid"] == item["qid"])
        assert row["latest_verdict"] is None
        degraded = [d for d in body["degraded"]
                    if d["code"] == "run_store_unreadable"]
        assert len(degraded) == 1, "读不出的降级条目必须去重"


# ── run / generate task plumbing ─────────────────────────


class TestRun:
    async def test_run_requires_confirmation(self, admin_client):
        r = await admin_client.post(f"{BASE}/run", json={})
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "confirmation_required"
        assert detail["tier"] == "verified"
        assert detail["n_items"] == 0
        assert detail["model"] == "mock/model"

    async def test_run_with_confirmation_completes(self, admin_client):
        item = await _create_item(admin_client)
        r = await admin_client.post(f"{BASE}/{item['qid']}/confirm")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "confirmed"

        # 未确认的正式运行仍然被门拦住,且 n_items 数的是该 tier 的已确认条目。
        blocked = await admin_client.post(f"{BASE}/run", json={})
        assert blocked.status_code == 400, blocked.text
        assert blocked.json()["detail"]["n_items"] == 1

        r = await admin_client.post(
            f"{BASE}/run", json={"confirm": True, "compile_only": True})
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "running"
        assert r.json()["task_id"]

        status = await _wait_status(admin_client, f"{BASE}/run/status")
        assert status["status"] == "done"
        assert status["kind"] == "evalset_run"
        assert status["summary"] == "exit=0"


class TestGenerate:
    async def test_second_generate_while_running_is_409(self, admin_client,
                                                       monkeypatch):
        from trove.api.routers import evalset as evalset_router
        from trove.services.evalset.generate import GenerateReport

        gate = asyncio.Event()

        async def _slow(_root, request, **_kwargs):
            await gate.wait()
            return GenerateReport(
                datasource=request.datasource, status="empty",
                degraded=[{"code": "evalset.busy", "message": "another writer"}],
            )

        monkeypatch.setattr(evalset_router, "generate_evalset", _slow)

        first = await admin_client.post(
            f"{BASE}/generate", json={"sources": ["semantic"]})
        assert first.status_code == 202, first.text
        second = await admin_client.post(
            f"{BASE}/generate", json={"sources": ["semantic"]})
        assert second.status_code == 409, second.text

        gate.set()
        status = await _wait_status(admin_client, f"{BASE}/generate/status")
        assert status["status"] == "done"
        assert status["kind"] == "evalset"
        # R20:busy 是**完成报告**里的事实,status 端点必须原样暴露解析后的报告,
        # 不能把它翻译成「没东西可生成」。
        assert status["report"]["status"] == "empty"
        assert status["report"]["degraded"][0]["code"] == "evalset.busy"

    async def test_status_filters_by_task_kind(self, admin_client):
        task = init_tasks.create(DS, "", kind="init")
        assert task is not None, "a fresh datasource must accept one task"
        init_tasks.done(task["id"], "kb init done")

        generate = await admin_client.get(f"{BASE}/generate/status")
        assert generate.status_code == 200
        assert generate.json() == {"status": "idle"}
        run = await admin_client.get(f"{BASE}/run/status")
        assert run.status_code == 200
        assert run.json() == {"status": "idle"}


# ── preview ──────────────────────────────────────────────


class TestPreview:
    async def test_masking_refusal_has_no_rows(self, admin_client, evalset_app):
        item = await _create_item(admin_client)
        # masking 默认开启;模型读不出来 → 取数之前拒绝(服务层契约)。
        evalset_app.state.semantic_layer = SimpleNamespace(model=lambda: None)

        r = await admin_client.post(f"{BASE}/{item['qid']}/preview")
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "evalset.masking_refused"
        assert "rows" not in r.text
