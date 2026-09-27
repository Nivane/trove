"""Decision-rule admin API tests (real KB on tmp dirs, zero LLM/network)."""

from __future__ import annotations

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from trove.api.app import create_app
from trove.core.config import AgentConfig
from trove.llm.gateway import LLMGateway
from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.config_store import ConfigStore
from trove.services.jobs.service import JobsService
from trove.services.jobs.store import JobStore
from trove.services.kb.service import KbService

RULE = {
    "id": "loan-drop",
    "name": "贷款余额环比下滑",
    "window": "本月",
    "subject": {"metrics": ["loan_balance"], "dimensions": ["region"]},
    "baseline": {"kind": "prev_period"},
    "scope": "per_dimension",
    "conditions": ["delta_pct < -0.1"],
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
async def decisions_app(sqlite_registry, tmp_path, auth_service):
    kb = KbService(tmp_path / "proj")
    kb.kb_dir.mkdir(parents=True)
    await kb.ensure_synced(None)
    jobs = JobsService(JobStore(tmp_path))
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
    })
    app.state.jobs = jobs
    yield app
    await jobs.store.dispose()


@pytest.fixture
async def admin_client(decisions_app, admin_token):
    transport = ASGITransport(app=decisions_app)
    headers = {"Authorization": f"Bearer {admin_token}"}
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers=headers) as c:
        yield c


@pytest.fixture
async def user_client(decisions_app, user_token):
    transport = ASGITransport(app=decisions_app)
    headers = {"Authorization": f"Bearer {user_token}"}
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers=headers) as c:
        yield c


def _write(app, rules, datasource="demo"):
    path = app.state.kb.decisions_path(datasource)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": 1, "rules": rules},
                                   allow_unicode=True), encoding="utf-8")


class TestList:
    async def test_empty_datasource_is_an_empty_list(self, admin_client):
        r = await admin_client.get("/v1/admin/decisions?datasource=demo")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rules"] == []
        assert any("no decision rules" in i for i in body["issues"])

    async def test_lists_rules_with_their_lint_state(self, admin_client, decisions_app):
        _write(decisions_app, [RULE])
        r = await admin_client.get("/v1/admin/decisions?datasource=demo")
        body = r.json()
        assert body["issues"] == []
        assert [x["id"] for x in body["rules"]] == ["loan-drop"]
        assert body["rules"][0]["severity"] == "warning"
        assert body["rules"][0]["conditions"] == ["delta_pct < -0.1"]
        assert body["digest"], "the digest is what a run records as 'which version'"

    async def test_a_corrupt_file_is_422_not_an_empty_list(
            self, admin_client, decisions_app):
        """Reporting "no rules" here would prompt the UI to offer creating a
        first rule — and the save would overwrite whatever is in the file."""
        path = decisions_app.state.kb.decisions_path("demo")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("rules: [{id: a}, {id: a}]", encoding="utf-8")
        r = await admin_client.get("/v1/admin/decisions?datasource=demo")
        assert r.status_code == 422

    async def test_flags_lint_issues(self, admin_client, decisions_app):
        _write(decisions_app, [{**RULE, "conditions": ["contribution > 1"]}])
        body = (await admin_client.get("/v1/admin/decisions?datasource=demo")).json()
        # per_dimension → contribution is fine; so use a rule that isn't
        assert body["issues"] == []

        _write(decisions_app, [{**RULE, "baseline": {"kind": "none"}}])
        body = (await admin_client.get("/v1/admin/decisions?datasource=demo")).json()
        assert any("delta" in i for i in body["issues"])

    async def test_reports_which_jobs_reference_a_rule(
            self, admin_client, decisions_app):
        _write(decisions_app, [RULE])
        await decisions_app.state.jobs.create_job(
            "贷款余额环比", "30", "interval", datasource="demo",
            decision_rule="loan-drop")
        body = (await admin_client.get("/v1/admin/decisions?datasource=demo")).json()
        assert len(body["rules"][0]["referenced_by"]) == 1
        assert "贷款余额环比" in body["rules"][0]["referenced_by"][0]

    async def test_a_job_on_another_datasource_is_not_a_reference(
            self, admin_client, decisions_app):
        _write(decisions_app, [RULE])
        await decisions_app.state.jobs.create_job(
            "q", "30", "interval", datasource="financial",
            decision_rule="loan-drop")
        body = (await admin_client.get("/v1/admin/decisions?datasource=demo")).json()
        assert body["rules"][0]["referenced_by"] == []


class TestGetOne:
    async def test_returns_the_rule_as_yaml_shape(self, admin_client, decisions_app):
        _write(decisions_app, [RULE])
        r = await admin_client.get("/v1/admin/decisions/loan-drop?datasource=demo")
        assert r.status_code == 200, r.text
        rule = r.json()["rule"]
        assert rule["id"] == "loan-drop"
        # round-trips through rule_to_dict → parse_rule, so the editor sees
        # exactly the mapping it can send back
        from trove.services.decision.rules import parse_rule

        assert parse_rule(rule).conditions == ["delta_pct < -0.1"]

    async def test_missing_rule_404(self, admin_client, decisions_app):
        _write(decisions_app, [RULE])
        r = await admin_client.get("/v1/admin/decisions/nope?datasource=demo")
        assert r.status_code == 404
        assert "nope" in r.json()["detail"]


class TestPut:
    async def test_writes_and_returns_the_git_result(self, admin_client, decisions_app):
        r = await admin_client.put("/v1/admin/decisions", json={
            "datasource": "demo", "rules": [RULE]})
        assert r.status_code == 200, r.text
        assert r.json()["rules"] == 1
        assert "git" in r.json()

        # ...and it is readable back through the KB
        doc = decisions_app.state.kb.load_decisions("demo")
        assert [x.id for x in doc.rules] == ["loan-drop"]
        assert doc.digest

    async def test_rejects_a_document_that_fails_lint(self, admin_client, decisions_app):
        """The lint gate is the whole point: a rule that can never fire is
        rejected at write time, not persisted as a dead scheduled job."""
        r = await admin_client.put("/v1/admin/decisions", json={
            "datasource": "demo",
            "rules": [{**RULE, "baseline": {"kind": "none"}}],
        })
        assert r.status_code == 400
        assert "baseline" in r.json()["detail"] or "delta" in r.json()["detail"]
        # nothing was written
        assert decisions_app.state.kb.load_decisions("demo").rules == []

    async def test_rejects_an_unparseable_condition(self, admin_client, decisions_app):
        r = await admin_client.put("/v1/admin/decisions", json={
            "datasource": "demo",
            "rules": [{**RULE, "conditions": ["dleta < 0"]}],
        })
        assert r.status_code == 400
        assert "dleta" in r.json()["detail"]

    async def test_rejects_duplicate_ids(self, admin_client, decisions_app):
        r = await admin_client.put("/v1/admin/decisions", json={
            "datasource": "demo", "rules": [RULE, {**RULE, "name": "other"}]})
        assert r.status_code == 400
        assert "duplicate" in r.json()["detail"]

    async def test_replacing_removes_what_is_gone(self, admin_client, decisions_app):
        _write(decisions_app, [RULE, {**RULE, "id": "other"}])
        r = await admin_client.put("/v1/admin/decisions", json={
            "datasource": "demo", "rules": [RULE]})
        assert r.status_code == 200
        doc = decisions_app.state.kb.load_decisions("demo")
        assert [x.id for x in doc.rules] == ["loan-drop"]

    async def test_empty_list_clears_the_file(self, admin_client, decisions_app):
        _write(decisions_app, [RULE])
        r = await admin_client.put("/v1/admin/decisions", json={
            "datasource": "demo", "rules": []})
        assert r.status_code == 200
        assert decisions_app.state.kb.load_decisions("demo").rules == []

    async def test_does_not_touch_a_neighbouring_semantics_file(
            self, admin_client, decisions_app):
        """`git_commit` defaults to globbing every *.yml in the datasource dir,
        which would fold someone's uncommitted semantics edit into a commit
        labelled "rule change"."""
        kb = decisions_app.state.kb
        kb.semantics_path("demo").parent.mkdir(parents=True, exist_ok=True)
        kb.semantics_path("demo").write_text("sentinel: true\n", encoding="utf-8")
        r = await admin_client.put("/v1/admin/decisions", json={
            "datasource": "demo", "rules": [RULE]})
        assert r.status_code == 200
        assert kb.semantics_path("demo").read_text(encoding="utf-8") == "sentinel: true\n"


class TestAuth:
    async def test_admin_only(self, user_client):
        assert (await user_client.get(
            "/v1/admin/decisions?datasource=demo")).status_code == 403
        assert (await user_client.put("/v1/admin/decisions", json={
            "datasource": "demo", "rules": []})).status_code == 403

    async def test_requires_auth(self, decisions_app):
        transport = ASGITransport(app=decisions_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/v1/admin/decisions?datasource=demo")
            assert r.status_code == 401
