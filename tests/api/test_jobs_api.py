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


class TestDecisionRuleReference:
    """A dangling rule reference must be rejected at write time — the runner
    would otherwise fail the same way on every tick, forever."""

    @staticmethod
    def _write_rules(app, datasource="demo", rules=None):
        import yaml

        rules = rules if rules is not None else [{
            "id": "loan-drop", "name": "贷款余额环比下滑", "window": "本月",
            "subject": {"metrics": ["loan_balance"], "dimensions": ["region"]},
            "baseline": {"kind": "prev_period"},
            "scope": "per_dimension",
            "conditions": ["delta_pct < -0.1"],
        }]
        path = app.state.kb.decisions_path(datasource)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump({"version": 1, "rules": rules}, allow_unicode=True),
            encoding="utf-8")

    async def test_create_with_a_declared_rule(self, admin_client, jobs_app):
        self._write_rules(jobs_app)
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "贷款余额环比", "schedule": "30",
            "datasource": "demo", "decision_rule": "loan-drop",
        })
        assert r.status_code == 201, r.text
        assert r.json()["job"]["decision_rule"] == "loan-drop"

    async def test_create_rejects_an_undeclared_rule(self, admin_client, jobs_app):
        self._write_rules(jobs_app)
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30", "decision_rule": "nope",
        })
        assert r.status_code == 400
        assert "nope" in r.json()["detail"]
        assert "loan-drop" in r.json()["detail"]      # says what *is* declared

    async def test_create_rejects_a_rule_with_no_decisions_file(
            self, admin_client, jobs_app):
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30", "decision_rule": "loan-drop",
        })
        assert r.status_code == 400
        assert "none declared" in r.json()["detail"]

    async def test_create_rejects_a_disabled_rule(self, admin_client, jobs_app):
        self._write_rules(jobs_app, rules=[{
            "id": "loan-drop", "name": "n", "enabled": False, "window": "本月",
            "subject": {"metrics": ["loan_balance"]},
            "baseline": {"kind": "prev_period"},
            "conditions": ["current > 0"],
        }])
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30", "decision_rule": "loan-drop",
        })
        assert r.status_code == 400
        assert "disabled" in r.json()["detail"]

    async def test_create_surfaces_a_corrupt_decisions_file(
            self, admin_client, jobs_app):
        """A file that will not parse is not "no rules" — a job created
        against it would fail on the first tick with nobody the wiser."""
        path = jobs_app.state.kb.decisions_path("demo")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("rules: [{id: a}, {id: a}]", encoding="utf-8")
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30", "decision_rule": "a",
        })
        assert r.status_code == 400
        assert "invalid" in r.json()["detail"]

    async def test_a_plain_job_needs_no_rule(self, admin_client, jobs_app):
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30",
        })
        assert r.status_code == 201
        assert r.json()["job"]["decision_rule"] == ""

    async def test_patch_can_attach_and_detach(self, admin_client, jobs_app):
        self._write_rules(jobs_app)
        job = await _make_job(jobs_app)
        assert job.decision_rule == ""

        attached = await admin_client.patch(f"/v1/admin/jobs/{job.id}", json={
            "decision_rule": "loan-drop"})
        assert attached.status_code == 200, attached.text
        assert attached.json()["job"]["decision_rule"] == "loan-drop"

        detached = await admin_client.patch(f"/v1/admin/jobs/{job.id}", json={
            "decision_rule": ""})
        assert detached.json()["job"]["decision_rule"] == ""

    async def test_patch_rejects_an_undeclared_rule(self, admin_client, jobs_app):
        self._write_rules(jobs_app)
        job = await _make_job(jobs_app)
        r = await admin_client.patch(f"/v1/admin/jobs/{job.id}", json={
            "decision_rule": "nope"})
        assert r.status_code == 400
        # ...and the job is untouched
        assert (await jobs_app.state.jobs.get_job(job.id)).decision_rule == ""


class TestTopicReference:
    """Same write-time discipline as decision_rule: a job whose topic domain
    can only fail at every tick must be rejected when it is created, not
    discovered by a silent daily failure."""

    @staticmethod
    def _write_model(app, datasource="demo"):
        from tests.helpers.kb import topic_model_yaml

        path = app.state.kb.semantics_path(datasource)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            topic_model_yaml(
                ["loan", "account"],
                # legacy 声明的数据集已从模型退役 → 域过期(empty_scope)
                {"loans": ["loan", "account"], "legacy": ["mortgage"]},
            ),
            encoding="utf-8")

    async def test_create_with_a_declared_topic(self, admin_client, jobs_app):
        self._write_model(jobs_app)
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "每月贷款总量", "schedule": "30",
            "datasource": "demo", "topic": "loans",
        })
        assert r.status_code == 201, r.text
        assert r.json()["job"]["topic"] == "loans"

    async def test_create_rejects_an_unknown_topic(self, admin_client, jobs_app):
        self._write_model(jobs_app)
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30", "topic": "nope",
        })
        assert r.status_code == 400
        assert "unknown topic" in r.json()["detail"]
        assert "loans" in r.json()["detail"]      # says what *is* declared

    async def test_create_rejects_an_expired_topic(self, admin_client, jobs_app):
        """域还在声明里,但它的数据集已全部退役 —— empty_scope 不是
        「不限定」:静默放宽会让任务天天答在作者没选的范围内。"""
        self._write_model(jobs_app)
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30", "topic": "legacy",
        })
        assert r.status_code == 400
        assert "expired" in r.json()["detail"]

    async def test_create_rejects_a_topic_with_no_semantic_model(
            self, admin_client, jobs_app):
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30", "topic": "loans",
        })
        assert r.status_code == 400
        assert "no semantic model" in r.json()["detail"]

    async def test_a_plain_job_needs_no_topic(self, admin_client, jobs_app):
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30",
        })
        assert r.status_code == 201
        assert r.json()["job"]["topic"] == ""

    async def test_patch_can_attach_and_detach(self, admin_client, jobs_app):
        self._write_model(jobs_app)
        job = await _make_job(jobs_app)
        assert job.topic == ""

        attached = await admin_client.patch(f"/v1/admin/jobs/{job.id}", json={
            "topic": "loans"})
        assert attached.status_code == 200, attached.text
        assert attached.json()["job"]["topic"] == "loans"

        detached = await admin_client.patch(f"/v1/admin/jobs/{job.id}", json={
            "topic": ""})
        assert detached.json()["job"]["topic"] == ""

    async def test_patch_rejects_an_unknown_topic(self, admin_client, jobs_app):
        self._write_model(jobs_app)
        job = await _make_job(jobs_app)
        r = await admin_client.patch(f"/v1/admin/jobs/{job.id}", json={
            "topic": "nope"})
        assert r.status_code == 400
        # ...and the job is untouched
        assert (await jobs_app.state.jobs.get_job(job.id)).topic == ""

    async def test_patch_judges_the_end_state_datasource(
            self, admin_client, jobs_app):
        """一次 PATCH 同时换源与设主题域时,判的是改完之后的组合:目标源
        没有语义模型 → 400,而不是按旧源的模型放行。"""
        self._write_model(jobs_app, "demo")
        job = await _make_job(jobs_app)
        r = await admin_client.patch(f"/v1/admin/jobs/{job.id}", json={
            "datasource": "other", "topic": "loans"})
        assert r.status_code == 400
        assert "no semantic model" in r.json()["detail"]


class TestScanSpecReference:
    """Same write-time discipline as decision_rule/topic — and it matters
    more here: a scan spec names metric × dimension *inside* one datasource's
    semantic model, so a typo is a dangling reference the job would hit on
    every tick, turning into a forever-growing pile of ``unverifiable`` rows.
    The reference check resolves names against the model only (zero queries)."""

    SPEC = {"metrics": ["loan_balance"], "dimensions": ["region"],
            "window": "本月", "lookback": 4}

    @staticmethod
    def _write_model(app, datasource="demo"):
        import yaml

        doc = {"semantic_model": [{
            "name": datasource,
            "datasets": [{
                "name": "loan", "source": "loan",
                "fields": [
                    {"name": "region", "datatype": "TEXT",
                     "semantic_role": "dimension",
                     "expression": {"dialects": [
                         {"dialect": "sqlite", "expression": "loan.region"}]}},
                    {"name": "amount", "datatype": "DOUBLE",
                     "semantic_role": "measure",
                     "expression": {"dialects": [
                         {"dialect": "sqlite", "expression": "loan.amount"}]}},
                    {"name": "date", "datatype": "DATE",
                     "semantic_role": "time",
                     "expression": {"dialects": [
                         {"dialect": "sqlite", "expression": "loan.date"}]}},
                ],
            }],
            "metrics": [{
                "name": "loan_balance",
                "expression": {"dialects": [
                    {"dialect": "sqlite", "expression": "SUM(loan.amount)"}]},
                "agg_time_dimension": "loan.date",
            }],
        }]}
        path = app.state.kb.semantics_path(datasource)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")

    async def test_create_with_a_declared_spec(self, admin_client, jobs_app):
        self._write_model(jobs_app)
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "贷款余额扫描", "schedule": "30",
            "datasource": "demo", "scan_spec": self.SPEC,
        })
        assert r.status_code == 201, r.text
        # 读侧解析回 dict(前端拿到形状,而不是一串要二次解析的 JSON)
        assert r.json()["job"]["scan_spec"]["metrics"] == ["loan_balance"]

    async def test_create_rejects_an_unknown_metric(self, admin_client, jobs_app):
        self._write_model(jobs_app)
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30",
            "scan_spec": {**self.SPEC, "metrics": ["nope"]},
        })
        assert r.status_code == 400
        assert "unknown metric 'nope'" in r.json()["detail"]
        # ...and nothing was written(否则那条任务每 tick 静默产 unverifiable)
        assert (await admin_client.get("/v1/admin/jobs")).json()["total"] == 0

    async def test_create_rejects_an_unknown_dimension(self, admin_client, jobs_app):
        self._write_model(jobs_app)
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30",
            "scan_spec": {**self.SPEC, "dimensions": ["nope"]},
        })
        assert r.status_code == 400
        assert "unknown dimension 'nope'" in r.json()["detail"]

    async def test_create_rejects_a_spec_with_no_semantic_model(
            self, admin_client, jobs_app):
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30", "scan_spec": self.SPEC,
        })
        assert r.status_code == 400
        assert "no semantic model" in r.json()["detail"]

    async def test_create_rejects_a_bad_structure(self, admin_client, jobs_app):
        """结构错是**写入时**的错误:一条挂空 metric 列表的规格宁可在
        创建时被拒,也不要每个 tick 以同样的方式静默失败。"""
        self._write_model(jobs_app)
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30", "scan_spec": {"metrics": []},
        })
        assert r.status_code == 400
        assert "invalid scan_spec" in r.json()["detail"]

        bad_mode = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30",
            "scan_spec": {**self.SPEC, "mode": "sideways"},
        })
        assert bad_mode.status_code == 400

    async def test_empty_spec_is_not_a_scan_job(self, admin_client, jobs_app):
        r = await admin_client.post("/v1/admin/jobs", json={
            "question": "q", "schedule": "30", "scan_spec": {},
        })
        assert r.status_code == 201
        assert r.json()["job"]["scan_spec"] == {}

    async def test_patch_can_attach_and_detach(self, admin_client, jobs_app):
        self._write_model(jobs_app)
        job = await _make_job(jobs_app)
        assert job.scan_spec == ""

        attached = await admin_client.patch(f"/v1/admin/jobs/{job.id}", json={
            "scan_spec": self.SPEC})
        assert attached.status_code == 200, attached.text
        assert attached.json()["job"]["scan_spec"]["window"] == "本月"

        detached = await admin_client.patch(f"/v1/admin/jobs/{job.id}", json={
            "scan_spec": {}})
        assert detached.json()["job"]["scan_spec"] == {}

    async def test_patch_rejects_an_unknown_metric(self, admin_client, jobs_app):
        self._write_model(jobs_app)
        job = await _make_job(jobs_app)
        r = await admin_client.patch(f"/v1/admin/jobs/{job.id}", json={
            "scan_spec": {**self.SPEC, "metrics": ["nope"]}})
        assert r.status_code == 400
        # ...and the job is untouched
        assert (await jobs_app.state.jobs.get_job(job.id)).scan_spec == ""

    async def test_patch_judges_the_end_state_datasource(
            self, admin_client, jobs_app):
        """一次 PATCH 同时换源与设扫描规格时,判的是改完之后的组合。"""
        self._write_model(jobs_app, "demo")
        job = await _make_job(jobs_app)
        r = await admin_client.patch(f"/v1/admin/jobs/{job.id}", json={
            "datasource": "other", "scan_spec": self.SPEC})
        assert r.status_code == 400
        assert "no semantic model" in r.json()["detail"]


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
