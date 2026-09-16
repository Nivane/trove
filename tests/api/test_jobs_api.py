"""Jobs admin API tests (real JobStore + fake scheduler, zero LLM/network)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from trove.api.app import create_app
from trove.core.config import AgentConfig
from trove.llm.gateway import LLMGateway
from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.config_store import ConfigStore
from trove.services.jobs.runner import SchedulerRunner
from trove.services.jobs.service import JobsService
from trove.services.jobs.store import JobStore
from trove.services.kb.service import KbService


class FakeScheduler(SchedulerRunner):
    """Duck-typed scheduler that records run_job_now calls instead of executing."""

    def __init__(self):
        self.called: list[str] = []

    async def run_job_now(self, job_id: str):
        self.called.append(job_id)
        return {
            "job_id": job_id, "name": "n", "status": "ok",
            "row_count": 3, "error": "", "alert": "", "alert_sent": False,
        }


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
async def jobs_app(sqlite_registry, tmp_path, auth_service):
    """App with jobs + fake scheduler wired in."""
    kb = KbService(tmp_path / "proj")
    kb.kb_dir.mkdir(parents=True)
    await kb.ensure_synced(None)
    jobs = JobsService(JobStore(tmp_path))
    scheduler = FakeScheduler()
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
        "scheduler": scheduler,
    })
    app.state.jobs = jobs
    app.state.scheduler = scheduler
    yield app
    await jobs.store.dispose()


@pytest.fixture
async def admin_client(jobs_app, admin_token):
    transport = ASGITransport(app=jobs_app)
    headers = {"Authorization": f"Bearer {admin_token}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


@pytest.fixture
async def user_client(jobs_app, user_token):
    transport = ASGITransport(app=jobs_app)
    headers = {"Authorization": f"Bearer {user_token}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


async def _make_job(app, question="每月贷款总量", schedule="30", schedule_type="interval"):
    return await app.state.jobs.create_job(
        question, schedule, schedule_type, datasource="demo",
    )


class TestJobsCRUD:
    async def test_create_and_list(self, admin_client, jobs_app):
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "每月贷款总量",
            "schedule": "30",
            "schedule_type": "interval",
            "datasource": "demo",
            "alert_expr": "row_count >= 5",
            "alert_channel": "console",
        })
        assert r.status_code == 201, r.text
        job = r.json()["job"]
        assert job["question"] == "每月贷款总量"
        assert job["schedule_type"] == "interval"
        assert job["enabled"] is True
        assert job["next_run_at"]

        listed = await admin_client.get("/v1/admin/jobs")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert listed.json()["jobs"][0]["id"] == job["id"]

    async def test_create_rejects_bad_schedule(self, admin_client):
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "99 99 99 99 99", "schedule_type": "cron",
        })
        assert r.status_code == 400
        assert "invalid" in r.json()["detail"]

    async def test_create_rejects_bad_channel(self, admin_client):
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30", "alert_channel": "slack:#x",
        })
        assert r.status_code == 400
        assert "alert_channel" in r.json()["detail"]

    async def test_get_and_patch(self, admin_client, jobs_app):
        job = await _make_job(jobs_app)
        got = await admin_client.get(f"/v1/admin/jobs/{job.id}")
        assert got.status_code == 200
        assert got.json()["job"]["id"] == job.id

        patched = await admin_client.patch(f"/v1/admin/jobs/{job.id}", json={
            "alert_expr": "value > 1000", "enabled": False,
        })
        assert patched.status_code == 200
        pj = patched.json()["job"]
        assert pj["alert_expr"] == "value > 1000"
        assert pj["enabled"] is False
        assert pj["next_run_at"] == ""

    async def test_patch_bad_schedule_400(self, admin_client, jobs_app):
        job = await _make_job(jobs_app)
        r = await admin_client.patch(f"/v1/admin/jobs/{job.id}", json={
            "schedule_type": "cron", "schedule": "99 99 99 99 99",
        })
        assert r.status_code == 400

    async def test_delete_and_404(self, admin_client, jobs_app):
        job = await _make_job(jobs_app)
        r = await admin_client.delete(f"/v1/admin/jobs/{job.id}")
        assert r.status_code == 204
        gone = await admin_client.get(f"/v1/admin/jobs/{job.id}")
        assert gone.status_code == 404

    async def test_get_missing_404(self, admin_client):
        assert (await admin_client.get("/v1/admin/jobs/nope")).status_code == 404


class TestJobsRun:
    async def test_run_now(self, admin_client, jobs_app):
        job = await _make_job(jobs_app)
        r = await admin_client.post(f"/v1/admin/jobs/{job.id}/run")
        assert r.status_code == 200
        assert r.json()["run"]["status"] == "ok"
        assert jobs_app.state.scheduler.called == [job.id]

    async def test_run_missing_404(self, admin_client):
        assert (await admin_client.post("/v1/admin/jobs/nope/run")).status_code == 404


class TestJobRunsHistory:
    async def test_list_runs(self, admin_client, jobs_app):
        job = await _make_job(jobs_app)
        svc = jobs_app.state.jobs
        run_id = await svc.record_run(job, await _run(job))
        await svc.finish_run(run_id, "alert", True, True, 7, "OK")
        r = await admin_client.get(f"/v1/admin/jobs/{job.id}/runs")
        assert r.status_code == 200
        runs = r.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["status"] == "alert"
        assert runs[0]["row_count"] == 7

    async def test_runs_missing_404(self, admin_client):
        assert (await admin_client.get("/v1/admin/jobs/nope/runs")).status_code == 404


class TestJobAuth:
    async def test_admin_only(self, user_client):
        assert (await user_client.get("/v1/admin/jobs")).status_code == 403
        assert (await user_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30",
        })).status_code == 403

    async def test_requires_auth(self, jobs_app):
        transport = ASGITransport(app=jobs_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            assert (await c.get("/v1/admin/jobs")).status_code == 401


async def _run(job):
    from trove.services.jobs.store import Run

    return Run(job_id=job.id)
