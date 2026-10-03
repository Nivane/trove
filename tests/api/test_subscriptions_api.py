"""Subscriptions API tests — admin CRUD + own-only user surface + 503 gating.

Real JobStore / JobsService / SubscriptionService over tmp SQLite, real auth
(admin + bob), zero LLM/network. 投递记录由直接调 ``deliver_for_run`` 造出
（投递发生在 runner 里，不经 HTTP；runner 侧的端到端在
tests/services/test_jobs_subscriptions.py）。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from trove.api.app import create_app
from trove.core.config import AgentConfig
from trove.llm.gateway import LLMGateway
from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.config_store import ConfigStore
from trove.services.jobs.service import JobsService
from trove.services.jobs.store import JobStore
from trove.services.jobs.subscribe import SubscriptionService
from trove.services.kb.service import KbService


@pytest.fixture
async def auth_service(tmp_path):
    from trove.services.auth.service import AuthService

    auth = AuthService(tmp_path / "app.db")
    await auth.ensure_bootstrap_admin(env_password="adminpw")
    await auth.create_user("bob", "bobpw", display_name="Bob")
    yield auth
    await auth.dispose()


@pytest.fixture
async def admin_token(auth_service):
    admin = await auth_service.authenticate("admin", "adminpw")
    raw, _ = await auth_service.create_token(admin["id"], label="test-admin")
    return raw


@pytest.fixture
async def user_token(auth_service):
    bob = await auth_service.authenticate("bob", "bobpw")
    raw, _ = await auth_service.create_token(bob["id"], label="test-bob")
    return raw


@pytest.fixture
async def sub_app(sqlite_registry, tmp_path, auth_service):
    """App with jobs + subscriptions wired in (no scheduler needed here)."""
    kb = KbService(tmp_path / "proj")
    kb.kb_dir.mkdir(parents=True)
    await kb.ensure_synced(None)
    jobs = JobsService(JobStore(tmp_path))
    subscriptions = SubscriptionService(jobs.store)
    app = create_app({
        "session_manager": None,
        "catalog_service": CatalogService(sqlite_registry),
        "connector_registry": sqlite_registry,
        "kb": kb,
        "auth": auth_service,
        "config_store": ConfigStore(tmp_path / "proj" / ".trove" / "datasources.yml"),
        "llm_gateway": LLMGateway(mock_response="x"),
        "config": AgentConfig(target="mock/model"),
        "jobs": jobs,
        "subscriptions": subscriptions,
    })
    yield app
    await jobs.store.dispose()


@pytest.fixture
async def admin_client(sub_app, admin_token):
    transport = ASGITransport(app=sub_app)
    headers = {"Authorization": f"Bearer {admin_token}"}
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers=headers) as c:
        yield c


@pytest.fixture
async def user_client(sub_app, user_token):
    transport = ASGITransport(app=sub_app)
    headers = {"Authorization": f"Bearer {user_token}"}
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers=headers) as c:
        yield c


async def _make_job(app, question="每月贷款总量", schedule="30"):
    job = await app.state.jobs.create_job(
        question, schedule, "interval", name="贷款日报", datasource="demo")
    assert job is not None
    return job


async def _deliver_once(app, job_id, *, run_id=1):
    job = await app.state.jobs.get_job(job_id)
    await app.state.subscriptions.deliver_for_run(
        job, run_id=run_id, status="ok", verdict="OK", alert_triggered=False,
        alert_message="", report={"answer": "本期报告", "question": job.question},
    )


class TestAdminSubscriptions:
    async def test_create_and_list(self, admin_client, sub_app):
        job = await _make_job(sub_app)
        r = await admin_client.post(
            f"/v1/admin/jobs/{job.id}/subscriptions",
            json={"subscriber": "bob"},
        )
        assert r.status_code == 201
        sub = r.json()["subscription"]
        assert sub["job_id"] == job.id
        assert sub["job_name"] == "贷款日报"
        assert sub["subscriber"] == "bob"
        assert sub["mode"] == "always"
        assert sub["enabled"] is True
        assert sub["channel"] == ""
        assert sub["created_by"] == "admin"

        r = await admin_client.get(f"/v1/admin/subscriptions?job_id={job.id}")
        assert r.status_code == 200
        assert r.json()["total"] == 1

        r = await admin_client.get("/v1/admin/subscriptions?subscriber=bob")
        assert r.json()["total"] == 1
        r = await admin_client.get("/v1/admin/subscriptions?subscriber=carol")
        assert r.json()["total"] == 0

    async def test_create_validations(self, admin_client, sub_app):
        job = await _make_job(sub_app)
        url = f"/v1/admin/jobs/{job.id}/subscriptions"

        r = await admin_client.post(url, json={"subscriber": "bob"})
        assert r.status_code == 201
        # 重复订阅 → 400
        r = await admin_client.post(url, json={"subscriber": "bob"})
        assert r.status_code == 400
        assert "already subscribed" in r.json()["detail"]
        # 不存在的用户 → 400（静默黑洞在写入时就拒掉）
        r = await admin_client.post(url, json={"subscriber": "carol"})
        assert r.status_code == 400
        assert "unknown user" in r.json()["detail"]
        # 空白 subscriber → 400
        r = await admin_client.post(url, json={"subscriber": " "})
        assert r.status_code == 400
        # 坏通道 → 400
        r = await admin_client.post(
            url, json={"subscriber": "admin", "channel": "webhook:nope"})
        assert r.status_code == 400
        assert "unsupported channel" in r.json()["detail"]
        # 不存在的任务 → 404
        r = await admin_client.post("/v1/admin/jobs/nope/subscriptions",
                                    json={"subscriber": "bob"})
        assert r.status_code == 404

    async def test_patch_and_delete(self, admin_client, sub_app):
        job = await _make_job(sub_app)
        r = await admin_client.post(f"/v1/admin/jobs/{job.id}/subscriptions",
                                    json={"subscriber": "bob"})
        sub_id = r.json()["subscription"]["id"]

        r = await admin_client.patch(f"/v1/admin/subscriptions/{sub_id}", json={
            "mode": "alert_only", "enabled": False, "channel": "console"})
        assert r.status_code == 200
        sub = r.json()["subscription"]
        assert (sub["mode"], sub["enabled"], sub["channel"]) == (
            "alert_only", False, "console")

        r = await admin_client.patch(f"/v1/admin/subscriptions/{sub_id}",
                                     json={"channel": "webhook:nope"})
        assert r.status_code == 400
        r = await admin_client.patch("/v1/admin/subscriptions/nope",
                                     json={"enabled": True})
        assert r.status_code == 404
        r = await admin_client.patch(f"/v1/admin/subscriptions/{sub_id}",
                                     json={"mode": "bogus"})
        assert r.status_code == 422  # Literal 挡在 schema 层

        r = await admin_client.delete(f"/v1/admin/subscriptions/{sub_id}")
        assert r.status_code == 204
        r = await admin_client.delete(f"/v1/admin/subscriptions/{sub_id}")
        assert r.status_code == 404

    async def test_delivery_listing(self, admin_client, sub_app):
        job = await _make_job(sub_app)
        r = await admin_client.post(f"/v1/admin/jobs/{job.id}/subscriptions",
                                    json={"subscriber": "bob"})
        sub_id = r.json()["subscription"]["id"]
        await _deliver_once(sub_app, job.id, run_id=1)

        r = await admin_client.get(f"/v1/admin/deliveries?job_id={job.id}")
        assert r.status_code == 200
        rows = r.json()["deliveries"]
        assert len(rows) == 1
        assert rows[0]["subscription_id"] == sub_id
        assert rows[0]["subscriber"] == "bob"
        assert rows[0]["status"] == "sent"
        assert rows[0]["excerpt"] == "本期报告"

        r = await admin_client.get("/v1/admin/deliveries?subscriber=nobody")
        assert r.json()["total"] == 0


class TestUserSubscriptions:
    async def test_my_list_only_mine(self, user_client, admin_client, sub_app):
        job = await _make_job(sub_app)
        await admin_client.post(f"/v1/admin/jobs/{job.id}/subscriptions",
                                json={"subscriber": "bob"})
        await admin_client.post(f"/v1/admin/jobs/{job.id}/subscriptions",
                                json={"subscriber": "admin"})

        r = await user_client.get("/v1/subscriptions")
        assert r.status_code == 200
        subs = r.json()["subscriptions"]
        assert [s["subscriber"] for s in subs] == ["bob"]
        assert subs[0]["job_name"] == "贷款日报"

    async def test_own_patch_and_delete(self, user_client, admin_client, sub_app):
        job = await _make_job(sub_app)
        bob_sub = (await admin_client.post(
            f"/v1/admin/jobs/{job.id}/subscriptions",
            json={"subscriber": "bob"})).json()["subscription"]
        admin_sub = (await admin_client.post(
            f"/v1/admin/jobs/{job.id}/subscriptions",
            json={"subscriber": "admin"})).json()["subscription"]

        r = await user_client.patch(f"/v1/subscriptions/{bob_sub['id']}",
                                    json={"mode": "alert_only"})
        assert r.status_code == 200
        assert r.json()["subscription"]["mode"] == "alert_only"

        # 别人的订阅对自己就是「不存在」——403 会变成存在性预言机
        r = await user_client.patch(f"/v1/subscriptions/{admin_sub['id']}",
                                    json={"enabled": False})
        assert r.status_code == 404
        r = await user_client.delete(f"/v1/subscriptions/{admin_sub['id']}")
        assert r.status_code == 404
        r = await user_client.get(
            f"/v1/subscriptions/{admin_sub['id']}/deliveries")
        assert r.status_code == 404

        r = await user_client.delete(f"/v1/subscriptions/{bob_sub['id']}")
        assert r.status_code == 204
        assert (await user_client.get("/v1/subscriptions")).json()["total"] == 0

    async def test_my_deliveries(self, user_client, admin_client, sub_app):
        job = await _make_job(sub_app)
        sub_id = (await admin_client.post(
            f"/v1/admin/jobs/{job.id}/subscriptions",
            json={"subscriber": "bob"})).json()["subscription"]["id"]
        await _deliver_once(sub_app, job.id, run_id=3)

        r = await user_client.get(f"/v1/subscriptions/{sub_id}/deliveries")
        assert r.status_code == 200
        rows = r.json()["deliveries"]
        assert len(rows) == 1
        assert rows[0]["run_id"] == 3

    async def test_user_cannot_create(self, user_client, sub_app):
        job = await _make_job(sub_app)
        r = await user_client.post(f"/v1/admin/jobs/{job.id}/subscriptions",
                                   json={"subscriber": "bob"})
        assert r.status_code == 403


class TestLayerGating:
    async def test_unwired_layer_is_503(self, client, api_app):
        """基础 api_app 不装 subscriptions 组件 → 明确 503，不是 404/500。"""
        r = await client.get("/v1/admin/subscriptions")
        assert r.status_code == 503
        r = await client.get("/v1/subscriptions")
        assert r.status_code == 503
        r = await client.post("/v1/admin/jobs/whatever/subscriptions",
                              json={"subscriber": "bob"})
        assert r.status_code == 503
