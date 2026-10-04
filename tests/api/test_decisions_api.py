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
async def verdict_store(decisions_app, tmp_path):
    """A real verdict history behind the app (the routes degrade to null
    without one, which is its own test)."""
    from trove.services.decision.verdict_store import VerdictStore

    store = VerdictStore(tmp_path / "proj")
    decisions_app.state.verdicts = store
    yield store
    await store.dispose()


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
        # Schema v3:未声明的规则**不带**这两个键(与历史列表逐字节一致)。
        assert "seasonal" not in body["rules"][0]
        assert "significance" not in body["rules"][0]

    async def test_a_declared_band_surfaces_in_the_list(
            self, admin_client, decisions_app):
        """声明了 significance 的规则要把两段带出来(与 ``rule_to_dict``
        同一序列化源)—— 列表是管理员判断「这条规则到底怎么判」的主视图,
        正文只在 YAML 编辑器里可见,列表不给等于看不见。"""
        _write(decisions_app, [{**RULE,
                                "seasonal": {"grain": "month", "lookback": 12,
                                             "mode": "trailing", "k": 3.5},
                                "significance": {"require": "outside_band"}}])
        r = await admin_client.get("/v1/admin/decisions?datasource=demo")
        body = r.json()
        assert body["issues"] == []
        rule = body["rules"][0]
        assert rule["seasonal"] == {"grain": "month", "lookback": 12,
                                    "mode": "trailing", "k": 3.5}
        assert rule["significance"] == {"require": "outside_band"}

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


RAW = """\
# 规则文件由管理端维护 —— 注释解释了每条阈值是怎么定下来的,
# 结构化往返会把它丢掉,所以编辑器整份文件读写。
version: 1
rules:
  - id: loan-drop
    name: 贷款余额环比下滑
    window: 本月
    subject:
      metrics: [loan_balance]
      dimensions: [region]
    baseline:
      kind: prev_period
    scope: per_dimension
    conditions:
      - delta_pct < -0.1
"""


class TestRawText:
    """The editor's file-shaped read/write pair.

    It exists so the admin UI needs no YAML parser of its own, which means
    the write path accepts a shape the structured path never sees — the lint
    gate has to hold here too.
    """

    async def test_raw_returns_the_file_verbatim(self, admin_client, decisions_app):
        path = decisions_app.state.kb.decisions_path("demo")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(RAW, encoding="utf-8")
        r = await admin_client.get("/v1/admin/decisions/raw?datasource=demo")
        assert r.status_code == 200, r.text
        assert r.json()["text"] == RAW

    async def test_raw_is_empty_for_a_datasource_with_no_file(self, admin_client):
        r = await admin_client.get("/v1/admin/decisions/raw?datasource=demo")
        assert r.status_code == 200
        assert r.json()["text"] == ""

    async def test_raw_wins_over_a_rule_actually_named_raw(
            self, admin_client, decisions_app):
        """`/raw` is registered before `/{rule_id}`; a rule with that id would
        otherwise shadow the editor's only read."""
        _write(decisions_app, [{**RULE, "id": "raw"}])
        r = await admin_client.get("/v1/admin/decisions/raw?datasource=demo")
        assert r.status_code == 200
        assert "text" in r.json()

    async def test_saving_text_writes_the_rules_it_declares(
            self, admin_client, decisions_app):
        r = await admin_client.put("/v1/admin/decisions", json={
            "datasource": "demo", "text": RAW})
        assert r.status_code == 200, r.text
        rule = decisions_app.state.kb.load_decisions("demo").rules[0]
        assert rule.id == "loan-drop"
        assert rule.conditions == ["delta_pct < -0.1"]

    async def test_a_save_canonicalizes_the_file(self, admin_client, decisions_app):
        """Read is verbatim, write is not — pinned deliberately.

        The write goes through ``_write_doc``, the KB's single write entry
        point, which re-stamps ``_meta``. Writing the text back untouched to
        keep the comments would leave the digest stale and the next read
        would report the file as human-edited. If comment preservation is
        ever added, it has to come with a digest story — and update this.
        """
        await admin_client.put("/v1/admin/decisions", json={
            "datasource": "demo", "text": RAW})
        back = await admin_client.get("/v1/admin/decisions/raw?datasource=demo")
        text = back.json()["text"]
        assert "注释解释了每条阈值" not in text
        assert "loan-drop" in text
        assert yaml.safe_load(text)["_meta"]["digest"]

    async def test_text_still_passes_the_lint_gate(self, admin_client, decisions_app):
        """A rule that can never fire is refused on the raw path exactly as on
        the structured one — otherwise the editor is a lint bypass."""
        r = await admin_client.put("/v1/admin/decisions", json={
            "datasource": "demo",
            "text": RAW.replace("kind: prev_period", "kind: none"),
        })
        assert r.status_code == 400
        assert decisions_app.state.kb.load_decisions("demo").rules == []

    async def test_text_that_is_not_yaml_is_400(self, admin_client, decisions_app):
        r = await admin_client.put("/v1/admin/decisions", json={
            "datasource": "demo", "text": "rules: [{"})
        assert r.status_code == 400
        assert "yaml" in r.json()["detail"].lower()

    async def test_text_that_is_not_a_mapping_is_400(self, admin_client):
        r = await admin_client.put("/v1/admin/decisions", json={
            "datasource": "demo", "text": "- just\n- a\n- list\n"})
        assert r.status_code == 400

    async def test_exactly_one_of_rules_or_text(self, admin_client):
        both = await admin_client.put("/v1/admin/decisions", json={
            "datasource": "demo", "rules": [RULE], "text": RAW})
        assert both.status_code == 400
        assert "exactly one" in both.json()["detail"]
        neither = await admin_client.put("/v1/admin/decisions", json={
            "datasource": "demo"})
        assert neither.status_code == 400


def _record(store, **over):
    from trove.services.decision.verdicts import VerdictRecord

    fields = dict(
        datasource="demo", rule_id="loan-drop", status="ok",
        triggered=False, rule_digest="sha256:a", evaluated_at="2026-09-01T00:00:00",
        created_at="2026-09-01T00:00:00", anchor_date="2026-09-01",
        evidence={"rows": [{"dim": "华东", "triggered": False,
                            "delta_pct": 0.05}]},
    )
    fields.update(over)
    return store.record(VerdictRecord(**fields))


class TestVerdictHistory:
    """判定历史 —— 审计线是决策层的产物,不是 run 行的事后解释。"""

    async def test_empty_history_is_an_empty_list_not_an_error(
            self, admin_client, verdict_store):
        r = await admin_client.get(
            "/v1/admin/decisions/loan-drop/verdicts?datasource=demo")
        assert r.status_code == 200, r.text
        assert r.json()["verdicts"] == []

    async def test_list_is_newest_first_with_inline_diffs(
            self, admin_client, verdict_store):
        await _record(verdict_store, evaluated_at="2026-09-01T00:00:00")
        await _record(verdict_store, triggered=True, status="alert",
                      message="[warning] 华东下滑",
                      rule_digest="sha256:b",
                      evaluated_at="2026-09-02T00:00:00")

        body = (await admin_client.get(
            "/v1/admin/decisions/loan-drop/verdicts?datasource=demo")).json()
        vs = body["verdicts"]
        assert [v["evaluated_at"] for v in vs] == [
            "2026-09-02T00:00:00", "2026-09-01T00:00:00"]
        top = vs[0]["diff"]
        assert top["trigger"] == "fired"
        assert top["rule_digest_changed"] is True
        assert top["status_change"] == ["ok", "alert"]
        assert vs[0]["diff"]["prev_id"] == vs[1]["id"]
        # 窗口最旧一条没有前一条可比 —— 是边界,不是"没变化"。
        assert vs[1]["diff"] is None

    async def test_list_rows_carry_no_evidence_blob(
            self, admin_client, verdict_store):
        """列表渲染不需要证据 SQL/原始行;它们让一次抽屉打开变成几百 KB。"""
        await _record(verdict_store)
        body = (await admin_client.get(
            "/v1/admin/decisions/loan-drop/verdicts?datasource=demo")).json()
        assert "evidence" not in body["verdicts"][0]
        assert body["verdicts"][0]["message"] == ""

    async def test_list_honours_limit_and_since(self, admin_client, verdict_store):
        await _record(verdict_store, evaluated_at="2026-09-01T00:00:00")
        await _record(verdict_store, evaluated_at="2026-09-02T00:00:00")

        body = (await admin_client.get(
            "/v1/admin/decisions/loan-drop/verdicts?datasource=demo"
            "&limit=1")).json()
        assert body["count"] == 1
        assert body["verdicts"][0]["evaluated_at"] == "2026-09-02T00:00:00"

        body = (await admin_client.get(
            "/v1/admin/decisions/loan-drop/verdicts?datasource=demo"
            "&since=2026-09-02T00:00:00")).json()
        assert body["count"] == 1

    async def test_detail_returns_the_evidence_and_the_diff(
            self, admin_client, verdict_store):
        await _record(verdict_store, evaluated_at="2026-09-01T00:00:00")
        vid = await _record(verdict_store, triggered=True, status="alert",
                            evaluated_at="2026-09-02T00:00:00",
                            evidence={"rows": [{"dim": "华东", "triggered": True,
                                                "delta_pct": -0.2}]})

        body = (await admin_client.get(
            f"/v1/admin/decisions/verdicts/{vid}")).json()
        assert body["verdict"]["id"] == vid
        assert body["verdict"]["evidence"]["rows"][0]["dim"] == "华东"
        assert body["diff"]["trigger"] == "fired"

    async def test_detail_of_the_very_first_verdict_has_no_diff(
            self, admin_client, verdict_store):
        vid = await _record(verdict_store)
        body = (await admin_client.get(
            f"/v1/admin/decisions/verdicts/{vid}")).json()
        assert body["diff"] is None

    async def test_unknown_verdict_is_404(self, admin_client, verdict_store):
        r = await admin_client.get("/v1/admin/decisions/verdicts/999")
        assert r.status_code == 404

    async def test_verdicts_route_is_not_swallowed_by_the_rule_id_pattern(
            self, admin_client, verdict_store):
        """`/verdicts/{id}` 必须排在 `/{rule_id}` 之前(同 `/raw` 的陷阱)。"""
        vid = await _record(verdict_store)
        r = await admin_client.get(f"/v1/admin/decisions/verdicts/{vid}")
        assert r.status_code == 200
        assert r.json()["verdict"]["id"] == vid

    async def test_list_attaches_the_latest_verdict_per_rule(
            self, admin_client, decisions_app, verdict_store):
        _write(decisions_app, [RULE, {**RULE, "id": "other"}])
        await _record(verdict_store, evaluated_at="2026-09-02T00:00:00",
                      message="最近一次")

        body = (await admin_client.get(
            "/v1/admin/decisions?datasource=demo")).json()
        by_id = {r["id"]: r for r in body["rules"]}
        assert by_id["loan-drop"]["latest_verdict"]["message"] == "最近一次"
        assert by_id["loan-drop"]["latest_verdict"]["diff"] is None
        assert by_id["other"]["latest_verdict"] is None

    async def test_a_broken_store_costs_the_history_not_the_rules(
            self, admin_client, decisions_app, monkeypatch):
        """历史不可用 ≠ 没有历史 —— 规则列表照常渲染。"""
        class _Boom:
            async def latest_for_rules(self, *_a, **_k):
                raise RuntimeError("store down")

        _write(decisions_app, [RULE])
        decisions_app.state.verdicts = _Boom()
        body = (await admin_client.get(
            "/v1/admin/decisions?datasource=demo")).json()
        assert [x["id"] for x in body["rules"]] == ["loan-drop"]
        assert body["rules"][0]["latest_verdict"] is None


class TestVerdictHistoryWithoutAStore:
    """没接 store 的进程(嵌入/测试):列表降级为 null,历史端点 409。"""

    async def test_list_degrades_to_null(self, admin_client, decisions_app):
        _write(decisions_app, [RULE])
        body = (await admin_client.get(
            "/v1/admin/decisions?datasource=demo")).json()
        assert body["rules"][0]["latest_verdict"] is None

    async def test_history_endpoints_are_409(self, admin_client):
        r = await admin_client.get(
            "/v1/admin/decisions/loan-drop/verdicts?datasource=demo")
        assert r.status_code == 409
        r = await admin_client.get("/v1/admin/decisions/verdicts/1")
        assert r.status_code == 409


class TestAuth:
    async def test_admin_only(self, user_client):
        assert (await user_client.get(
            "/v1/admin/decisions?datasource=demo")).status_code == 403
        assert (await user_client.put("/v1/admin/decisions", json={
            "datasource": "demo", "rules": []})).status_code == 403

    async def test_verdict_history_is_admin_only(self, user_client):
        """证据里是原始业务行 —— v1 只做 admin 面。"""
        assert (await user_client.get(
            "/v1/admin/decisions/loan-drop/verdicts?datasource=demo"
        )).status_code == 403
        assert (await user_client.get(
            "/v1/admin/decisions/verdicts/1")).status_code == 403

    async def test_requires_auth(self, decisions_app):
        transport = ASGITransport(app=decisions_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/v1/admin/decisions?datasource=demo")
            assert r.status_code == 401
