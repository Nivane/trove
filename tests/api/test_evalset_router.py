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


def _write_latest_entries(app, tier: str, entries: list[dict]) -> Path:
    """写 ``latest.jsonl``;条目与 runner 落盘形状一致(不含 tier 字段)。"""
    run_dir = (
        _project_root(app) / ".trove" / "eval" / "evalsets" / DS / tier
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "latest.jsonl"
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries)
        + "\n",
        encoding="utf-8",
    )
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

    async def test_tags_use_the_model_dict_shape(self, admin_client):
        body = await _create_item(
            admin_client, tags={"metric": True, "owner": "analytics"})
        assert body["tags"] == {"metric": True, "owner": "analytics"}

        # 列表形状在 pydantic 层就被拒(422),不会走到 dict(list) 的
        # 巧合语义(``["ab"]`` → ``{"a": "b"}``),更不会 500。
        r = await admin_client.post(
            f"{BASE}/items",
            json={"question": "另一个问题？",
                  "reference_sql": "SELECT 1", "tags": ["metric"]})
        assert r.status_code == 422, r.text

    async def test_patch_replaces_and_clears_tags(self, admin_client):
        item = await _create_item(admin_client, tags={"metric": True})
        r = await admin_client.patch(
            f"{BASE}/{item['qid']}", json={"tags": {"owner": "ops"}})
        assert r.status_code == 200, r.text
        assert r.json()["tags"] == {"owner": "ops"}

        # None = 不改,{} = 清空 —— 两个状态必须分得开。
        r = await admin_client.patch(
            f"{BASE}/{item['qid']}", json={"note": "reviewed"})
        assert r.json()["tags"] == {"owner": "ops"}
        r = await admin_client.patch(
            f"{BASE}/{item['qid']}", json={"tags": {}})
        assert r.status_code == 200, r.text
        assert r.json()["tags"] == {}

    async def test_write_under_a_held_lock_is_409(self, admin_client,
                                                  evalset_app):
        """真实锁竞争 → 409 evalset.busy(不是 500)。"""
        from trove.services.evalset.lock import EvalsetLock

        lock = EvalsetLock(_project_root(evalset_app) / ".trove" / "evalsets")
        with lock.acquire(DS):
            r = await admin_client.post(
                f"{BASE}/items",
                json={"question": "有多少学生？",
                      "reference_sql": "SELECT COUNT(*) FROM students"})
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "evalset.busy"
        assert detail["issues"] == []

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
        # 只有 verified 坏,降级条目只报 verified —— 不能把健康的 tier
        # 一起列为「结果读不出」。
        assert degraded[0]["tiers"] == ["verified"]

    async def test_verdict_comes_from_the_items_own_tier(
        self, admin_client, evalset_app,
    ):
        """同一 qid 在两层都有历史判定 → 条目只取自己那一层(M1)。"""
        verified = await _create_item(admin_client)
        semantic = await _create_item(
            admin_client,
            question="语义层的题？",
            sql="SELECT AVG(grade) FROM students",
        )

        # 把第二条改造成语义编译器产出 → tier 读时派生为 semantic。
        from trove.services.evalset.store import load_evalset, save_evalset

        root = _project_root(evalset_app)
        doc, _issues, _exists = load_evalset(root, DS)
        target = next(item for item in doc.items if item.qid == semantic["qid"])
        target.source = "semantic"
        target.reference_origin = "semantic_compiler"
        save_evalset(root, doc)

        # 两个 qid 在两层都有判定,结论相反;旧实现按固定 tier 顺序合并,
        # verified 条目会被 semantic 的旧结论覆盖成 MISMATCH。
        _write_latest_entries(evalset_app, "verified", [
            {"qid": verified["qid"], "run_id": "run-v", "verdict": "MATCH",
             "at": "2026-10-18T00:00:00+00:00"},
            {"qid": semantic["qid"], "run_id": "run-v", "verdict": "MATCH",
             "at": "2026-10-18T00:00:00+00:00"},
        ])
        _write_latest_entries(evalset_app, "semantic", [
            {"qid": verified["qid"], "run_id": "run-s", "verdict": "MISMATCH",
             "at": "2026-10-19T00:00:00+00:00"},
            {"qid": semantic["qid"], "run_id": "run-s", "verdict": "MISMATCH",
             "at": "2026-10-19T00:00:00+00:00"},
        ])

        r = await admin_client.get(BASE)
        assert r.status_code == 200, r.text
        rows = {row["qid"]: row for row in r.json()["items"]}
        assert rows[verified["qid"]]["tier"] == "verified"
        assert rows[verified["qid"]]["latest_verdict"]["verdict"] == "MATCH"
        assert rows[semantic["qid"]]["tier"] == "semantic"
        assert rows[semantic["qid"]]["latest_verdict"]["verdict"] == "MISMATCH"


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
        # 摘要携带结局(JSON),不再只是 "exit=0";status 端点给解析后的结果。
        summary = json.loads(status["summary"])
        assert summary["exit_code"] == 0
        assert status["result"]["exit_code"] == 0
        assert status["result"]["tier"] == "verified"
        # compile_only 不写 run 文件 → run_id 为空(而不是伪造一个)。
        assert status["result"]["run_id"] == ""

    async def test_gate_counts_only_exact_confirmed_items(self, admin_client,
                                                         evalset_app):
        """n_items 复刻 runner ``_select``:非 exact 的已确认条目不计数。"""
        first = await _create_item(
            admin_client, question="有多少学生？",
            sql="SELECT COUNT(*) FROM students")
        second = await _create_item(
            admin_client, question="平均分是多少？",
            sql="SELECT AVG(grade) FROM students")
        r = await admin_client.post(
            f"{BASE}/confirm", json={"qids": [first["qid"], second["qid"]]})
        assert r.status_code == 200, r.text

        # 手编资产里可能存在非 exact 的已确认条目(confirm 门挡的是新写入,
        # 历史数据不归它管)。用合法的 ``shape``:未知枚举是解析 issue,
        # 新门的「不可读资产拒跑」会先行拒绝,这里要钉的是题数口径。
        from trove.services.evalset.store import load_evalset, save_evalset

        root = _project_root(evalset_app)
        doc, _issues, _exists = load_evalset(root, DS)
        next(item for item in doc.items if item.qid == second["qid"]).match = (
            "shape")
        save_evalset(root, doc)

        r = await admin_client.post(f"{BASE}/run", json={})
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["n_items"] == 1


class TestUnreadableAssetRunGate:
    """C3 / R24:读不出的资产不能被读成「0 道题」再放行确认。"""

    async def test_every_run_flavor_is_refused(self, admin_client, evalset_app):
        asset = (
            _project_root(evalset_app) / ".trove" / "evalsets" / DS
            / "evalset.yml"
        )
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text("version: 1\nitems: [broken", encoding="utf-8")

        # 成本探针 / 已确认 / 零 LLM 自检 —— 一条都不能启动。
        for body in ({}, {"confirm": True}, {"compile_only": True}):
            r = await admin_client.post(f"{BASE}/run", json=body)
            assert r.status_code == 400, (body, r.text)
            detail = r.json()["detail"]
            assert detail["code"] == "evalset.invalid_asset", (body, detail)
            assert detail["issues"], "解析 issue 必须原样带回"
            # 关键:不可读资产绝不产出 n_items(尤其是 0)。
            assert "n_items" not in detail, (body, detail)
            assert "confirmation_required" != detail["code"]


class TestRunOutcome:
    """I1:任务摘要与 status 端点必须带 exit_code / message,不只「done」。"""

    async def test_exit_two_surfaces_message_and_no_run_id(
        self, admin_client, monkeypatch,
    ):
        from trove.api.routers import evalset as evalset_router
        from trove.eval.evalset_run import RunResult

        async def _exit_2(_options):
            return RunResult(
                exit_code=2, tier="verified",
                message="没有已确认的题目;先确认至少一道 exact 题",
            )

        monkeypatch.setattr(evalset_router, "run_evalset", _exit_2)
        r = await admin_client.post(f"{BASE}/run", json={"confirm": True})
        assert r.status_code == 202, r.text

        status = await _wait_status(admin_client, f"{BASE}/run/status")
        assert status["status"] == "done"
        assert status["result"]["exit_code"] == 2
        assert "没有已确认的题目" in status["result"]["message"]
        assert status["result"]["run_id"] == ""
        assert status["result"]["tier"] == "verified"

    async def test_exit_zero_carries_run_id(self, admin_client, monkeypatch):
        from trove.api.routers import evalset as evalset_router
        from trove.eval.evalset_run import RunResult

        async def _exit_0(_options):
            return RunResult(
                exit_code=0, run_id="evalset-ok", tier="verified",
                summary={"n": 1, "ex": 1.0}, skipped=0,
            )

        monkeypatch.setattr(evalset_router, "run_evalset", _exit_0)
        r = await admin_client.post(f"{BASE}/run", json={"confirm": True})
        assert r.status_code == 202, r.text

        status = await _wait_status(admin_client, f"{BASE}/run/status")
        assert status["status"] == "done"
        assert status["result"]["exit_code"] == 0
        assert status["result"]["run_id"] == "evalset-ok"
        assert status["result"]["skipped"] == 0


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

        r = await admin_client.get(f"{BASE}/{item['qid']}/preview")
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "evalset.masking_refused"
        assert "rows" not in r.text
        # R30:预览只留 GET,不再有 POST 写入口。
        post = await admin_client.post(f"{BASE}/{item['qid']}/preview")
        assert post.status_code == 405, post.text
