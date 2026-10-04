"""Decision-rule admin API tests (real KB on tmp dirs, zero LLM/network)."""

from __future__ import annotations

from types import SimpleNamespace

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
        # Schema v3/v4:未声明的规则**不带**这三个键(与历史列表逐字节一致)。
        assert "seasonal" not in body["rules"][0]
        assert "significance" not in body["rules"][0]
        assert "causal" not in body["rules"][0]

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

    async def test_a_declared_ladder_surfaces_in_the_list(
            self, admin_client, decisions_app):
        """schema v4:声明了 causal 的规则把升级梯声明带进列表 —— 管理员
        要看得见「这条规则的净效应是被什么对照与容差约束的」。"""
        _write(decisions_app, [{
            "id": "region-causal",
            "name": "华东贷款余额异动",
            "window": "本月",
            "subject": {"metrics": ["loan_balance"], "dimensions": [],
                        "filters": [{"field": "loan.region", "op": "=",
                                     "value": "华东"}]},
            "baseline": {"kind": "prev_period"},
            "scope": "aggregate",
            "conditions": ["delta_pct > 0.05"],
            "seasonal": {"lookback": 12},
            "causal": {"mode": "auto",
                       "control": {"dim": "region", "value": "华北"}},
        }])
        r = await admin_client.get("/v1/admin/decisions?datasource=demo")
        body = r.json()
        assert body["issues"] == [], body["issues"]
        assert body["rules"][0]["causal"] == {
            "mode": "auto", "control": {"dim": "region", "value": "华北"},
            "placebo_blocks": 4, "tolerance": 0.1}

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


class TestSimulate:
    """what-if 模拟 —— 零业务库查询是它的核心承诺(验收清单 4)。"""

    ROWS = [
        {"dim": "华东", "current": 600, "baseline": 1200, "delta": -600,
         "delta_pct": -0.5, "contribution": -0.92, "triggered": True,
         "matched": ["delta_pct < -0.1"]},
        {"dim": "华北", "current": 450, "baseline": 400, "delta": 50,
         "delta_pct": 0.125, "contribution": 0.08, "triggered": False,
         "matched": []},
    ]

    async def _digest(self, admin_client, datasource="demo"):
        body = (await admin_client.get(
            f"/v1/admin/decisions?datasource={datasource}")).json()
        return body["digest"]

    async def test_verdict_replay_judges_the_recorded_numbers_only(
            self, admin_client, decisions_app, verdict_store, monkeypatch):
        """重放最近 verdict 的行卡 → 同一条内核重判;**零业务库查询**。"""
        calls: list[str] = []

        async def _spy(sql, datasource=None):
            calls.append(sql)
            raise AssertionError("simulate must never touch the business DB")

        monkeypatch.setattr(
            decisions_app.state.connector_registry, "execute", _spy)
        _write(decisions_app, [RULE])
        vid = await _record(
            verdict_store, triggered=True, status="alert",
            rule_digest=await self._digest(admin_client),
            evidence={"rows": self.ROWS, "evidence": {"row_count": 2}})

        r = await admin_client.post(
            "/v1/admin/decisions/loan-drop/simulate?datasource=demo",
            json={"scenario": [{"dim": "华东", "field": "current",
                                "mode": "set", "value": 1200}]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert calls == []
        assert body["source"] == {"kind": "verdict", "verdict_id": vid,
                                  "evaluated_at": "2026-09-01T00:00:00",
                                  "rule_digest": await self._digest(admin_client),
                                  "stale": False,
                                  "row_count_source": "evidence"}
        assert body["before"]["triggered"] is True
        assert body["after"]["triggered"] is False
        assert body["flip"] == "cleared"
        assert body["applied"][0]["after"] == 1200
        assert body["summary"]["flip"] == "cleared"

    async def test_caller_maps_are_judged_directly(
            self, admin_client, decisions_app):
        _write(decisions_app, [RULE])
        r = await admin_client.post(
            "/v1/admin/decisions/loan-drop/simulate?datasource=demo",
            json={"source": "caller",
                  "current": {"华东": 1300, "华北": 450},
                  "baseline": {"华东": 1200, "华北": 400},
                  "scenario": [{"dim": "华北", "mode": "set", "value": 300}]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == {"kind": "caller"}
        assert body["flip"] == "fired"

    async def test_caller_mode_without_current_numbers_is_rejected(
            self, admin_client, decisions_app):
        _write(decisions_app, [RULE])
        r = await admin_client.post(
            "/v1/admin/decisions/loan-drop/simulate?datasource=demo",
            json={"source": "caller"})
        assert r.status_code == 400
        assert "current" in r.json()["detail"]

    async def test_aggregate_rule_rejects_dimension_keys_loudly(
            self, admin_client, decisions_app):
        """聚合规则唯一的组键是 '';别的键没被用上而模拟看起来跑过了 ——
        正是模拟面最不能有的静默失败。"""
        _write(decisions_app, [{**RULE, "id": "loan-high",
                                "scope": "aggregate",
                                "subject": {"metrics": ["loan_balance"]}}])
        r = await admin_client.post(
            "/v1/admin/decisions/loan-high/simulate?datasource=demo",
            json={"source": "caller", "current": {"华东": 100}})
        assert r.status_code == 400
        assert "aggregate" in r.json()["detail"]

    async def test_malformed_scenario_is_rejected_with_the_legal_keys(
            self, admin_client, decisions_app):
        _write(decisions_app, [RULE])
        r = await admin_client.post(
            "/v1/admin/decisions/loan-drop/simulate?datasource=demo",
            json={"source": "caller", "current": {"华东": 100},
                  "scenario": [{"dim": "华东", "value": 1, "modex": "pct"}]})
        assert r.status_code == 400
        assert "dim, field, mode, value" in r.json()["detail"]

    async def test_no_verdict_to_replay_is_404_with_the_way_out(
            self, admin_client, decisions_app, verdict_store):
        _write(decisions_app, [RULE])
        r = await admin_client.post(
            "/v1/admin/decisions/loan-drop/simulate?datasource=demo",
            json={"scenario": []})
        assert r.status_code == 404
        assert "source='caller'" in r.json()["detail"]

    async def test_caller_maps_with_verdict_source_are_refused(
            self, admin_client, decisions_app, verdict_store):
        """给了 maps 却重放 verdict = 用错了数据源而结果看起来是对的。"""
        _write(decisions_app, [RULE])
        await _record(verdict_store, evidence={"rows": self.ROWS})
        r = await admin_client.post(
            "/v1/admin/decisions/loan-drop/simulate?datasource=demo",
            json={"current": {"华东": 1}})
        assert r.status_code == 400
        assert "source='caller'" in r.json()["detail"]

    async def test_without_a_store_replay_is_409(
            self, admin_client, decisions_app):
        _write(decisions_app, [RULE])
        r = await admin_client.post(
            "/v1/admin/decisions/loan-drop/simulate?datasource=demo",
            json={"scenario": []})
        assert r.status_code == 409

    async def test_unknown_rule_is_404(self, admin_client, decisions_app):
        _write(decisions_app, [RULE])
        r = await admin_client.post(
            "/v1/admin/decisions/nope/simulate?datasource=demo", json={})
        assert r.status_code == 404

    async def test_a_rule_edited_since_the_verdict_is_flagged(
            self, admin_client, decisions_app, verdict_store):
        """数字是旧规则的判定现场,条件是新规则的 —— 必须看得见。"""
        _write(decisions_app, [RULE])
        await _record(verdict_store, rule_digest="sha256:stale",
                      evidence={"rows": self.ROWS})
        body = (await admin_client.post(
            "/v1/admin/decisions/loan-drop/simulate?datasource=demo",
            json={"scenario": []})).json()
        assert body["source"]["stale"] is True
        assert {"stage": "replay",
                "reason": "rule_edited_since_verdict"} in body["degraded"]

    async def test_impacts_without_tree_evidence_say_why(
            self, admin_client, decisions_app, verdict_store):
        _write(decisions_app, [RULE])
        await _record(verdict_store, evidence={"rows": self.ROWS})
        body = (await admin_client.post(
            "/v1/admin/decisions/loan-drop/simulate?datasource=demo",
            json={"scenario": [], "impacts": {"loan_a": -10}})).json()
        assert body["tree"]["total"] is None
        assert body["tree"]["not_modeled"][0]["reason"] == "no_tree_evidence"
        assert body["summary"]["total_delta"] is None
        # 老证据没记 row_count → 退到行卡条数,退让在 source 里看得见。
        assert body["source"]["row_count_source"] == "cards"

    async def test_impacts_over_the_replayed_driver_tree_sum_at_the_root(
            self, admin_client, decisions_app, verdict_store):
        tree = {"name": "loan_balance", "kind": "derived", "op": "+",
                "decomposable": True, "expression": "(+)", "children": [
                    {"name": "华东", "candidate": "loan_a", "kind": "leaf",
                     "expression": "loan_a", "decomposable": True,
                     "children": []},
                    {"name": "华北", "candidate": "loan_b", "kind": "leaf",
                     "expression": "loan_b", "decomposable": True,
                     "children": []}]}
        _write(decisions_app, [RULE])
        await _record(verdict_store,
                      evidence={"rows": self.ROWS,
                                "analysis": {"tree": tree}})
        body = (await admin_client.post(
            "/v1/admin/decisions/loan-drop/simulate?datasource=demo",
            json={"scenario": [],
                  "impacts": {"loan_a": -60, "loan_b": 20}})).json()
        assert body["tree"]["total"] == -40
        assert body["summary"]["total_delta"] == -40

    async def test_simulate_is_admin_only(self, user_client):
        r = await user_client.post(
            "/v1/admin/decisions/loan-drop/simulate?datasource=demo",
            json={"scenario": []})
        assert r.status_code == 403

    async def test_route_is_not_swallowed_by_the_rule_id_pattern(
            self, admin_client, decisions_app):
        """``/{rule_id}/simulate`` 必须排在 ``/{rule_id}`` 之前(同 /raw 的
        Starlette 顺序陷阱)—— 命中即 200,被吞会变成 405。"""
        _write(decisions_app, [RULE])
        r = await admin_client.post(
            "/v1/admin/decisions/loan-drop/simulate?datasource=demo",
            json={"source": "caller", "current": {"华东": 600}})
        assert r.status_code == 200
        # 既有 GET 不受影响
        assert (await admin_client.get(
            "/v1/admin/decisions/loan-drop?datasource=demo")).status_code == 200


def _rev(rule: dict) -> str:
    from trove.services.decision.rules import parse_rule, rule_rev

    return rule_rev(parse_rule(rule))


class _FakeEffectStore:
    """质量端点要的那一个方法 —— 签名必须与真 store 的
    ``list_effect_entries(datasource, *, limit)`` 一致,不一致这里先炸。"""

    def __init__(self, entries: list[dict]):
        self.entries = entries
        self.calls: list[tuple[str, int]] = []

    async def list_effect_entries(self, datasource: str, *, limit: int = 500):
        self.calls.append((datasource, limit))
        return list(self.entries)


def _effect_entry(rev: str, **over) -> dict:
    entry = {"rule_id": "loan-drop", "rule_rev": rev, "outside_band": True,
             "error": "", "measured_at": "2026-10-06T00:00:00",
             "window_end": "2026-10-05", "proposal_id": "p-1", "pct": 0.1}
    entry.update(over)
    return entry


class TestQualityReport:
    """判定质量回评 —— 分桶键是 ``(rule_id, rule_rev)``,不是整份文件的
    digest(编辑别的规则不该动这条规则的桶;验收清单 7 的 API 面)。"""

    async def test_verdicts_bucket_by_rev_and_annotate_against_the_file(
            self, admin_client, decisions_app, verdict_store):
        _write(decisions_app, [RULE])
        current = _rev(RULE)
        await _record(verdict_store, evaluated_at="2026-09-01T00:00:00",
                      evidence={"rows": [], "rule_rev": current})
        await _record(verdict_store, evaluated_at="2026-09-02T00:00:00",
                      evidence={"rows": [], "rule_rev": "rev-old"})
        await _record(verdict_store, evaluated_at="2026-09-03T00:00:00",
                      evidence={"rows": []})          # B2 之前的行,没有 rev

        body = (await admin_client.get(
            "/v1/admin/decisions/quality?datasource=demo")).json()
        assert body["verdicts_read"] == 3
        by_rev = {b["rule_rev"]: b for b in body["buckets"]}
        assert set(by_rev) == {current, "rev-old", "rev_unknown"}
        cur = by_rev[current]
        assert cur["total"] == 1 and cur["rule_declared"] is True
        assert cur["rule_rev_current"] is True
        assert by_rev["rev-old"]["rule_rev_current"] is False
        # 没版本信息的桶:「无从谈起」是第三种答案,不是 False。
        assert by_rev["rev_unknown"]["rule_rev_current"] is None
        assert body["summary"]["buckets"] == 3
        assert body["summary"]["total"] == 3

    async def test_editing_rule_b_does_not_clear_rule_a_bucket(
            self, admin_client, decisions_app, verdict_store):
        """验收清单 7:digest 会随任意编辑变,rev 只随这条规则变。"""
        rule_b = {**RULE, "id": "other", "conditions": ["delta_pct > 0.5"]}
        _write(decisions_app, [RULE, rule_b])
        rev_a = _rev(RULE)
        await _record(verdict_store, evidence={"rows": [], "rule_rev": rev_a})

        before = (await admin_client.get(
            "/v1/admin/decisions/quality?datasource=demo")).json()
        _write(decisions_app, [RULE, {**rule_b, "conditions": ["delta_pct > 0.9"]}])
        after = (await admin_client.get(
            "/v1/admin/decisions/quality?datasource=demo")).json()

        (b,) = after["buckets"]
        assert (b["rule_id"], b["rule_rev"]) == ("loan-drop", rev_a)
        assert b["rule_rev_current"] is True, "改 B 规则不改 A 的 rev"
        assert after["digest"] != before["digest"], \
            "digest 确实变了 —— 这正是它不能当分桶键的原因"

    async def test_a_deleted_rule_keeps_its_history_marked_not_declared(
            self, admin_client, decisions_app, verdict_store):
        """规则删了,那段判定史还在:桶照出现,``rule_declared`` 说出
        哪一半变了(历史是真的,只是不再对应任何当前规则)。"""
        _write(decisions_app, [])
        await _record(verdict_store, evidence={"rows": []})
        (b,) = (await admin_client.get(
            "/v1/admin/decisions/quality?datasource=demo")).json()["buckets"]
        assert b["total"] == 1
        assert b["rule_declared"] is False
        assert b["rule_rev_current"] is None

    async def test_the_action_layer_feeds_effect_columns_into_the_same_buckets(
            self, admin_client, decisions_app, verdict_store):
        _write(decisions_app, [RULE])
        rev = _rev(RULE)
        await _record(verdict_store, evidence={"rows": [], "rule_rev": rev})
        fake = _FakeEffectStore([
            _effect_entry(rev),
            _effect_entry(rev, outside_band=False, pct=-0.01,
                          measured_at="2026-10-07T00:00:00"),
            _effect_entry(rev, outside_band=None, error="verifier_failed: x",
                          measured_at="2026-10-08T00:00:00"),
        ])
        decisions_app.state.actions = SimpleNamespace(store=fake)

        body = (await admin_client.get(
            "/v1/admin/decisions/quality?datasource=demo")).json()
        assert body["degraded"] == []
        assert body["effects_read"] == 3
        assert fake.calls == [("demo", 500)], "按数据源过滤、带 limit 读"
        (b,) = body["buckets"]
        assert b["effects"] == {"measured": 2, "effective": 1, "no_effect": 1,
                                "unverifiable": 0, "errors": 1}
        assert b["decided"] == 2 and b["effective_rate"] is None
        assert "few_effects" in b["insufficient"]
        assert body["summary"]["effects"]["effective"] == 1
        assert body["summary"]["decided"] == 2

    async def test_without_the_action_layer_the_verdict_half_still_answers(
            self, admin_client, decisions_app, verdict_store):
        """「没有效果数据」绝不能被读成「没有效果」—— 判定那一半照答,
        缺的那条腿在 degraded 里说得出来。"""
        _write(decisions_app, [RULE])
        await _record(verdict_store, evidence={"rows": []})
        body = (await admin_client.get(
            "/v1/admin/decisions/quality?datasource=demo")).json()
        assert body["effects_read"] == 0
        assert {"stage": "effects", "reason": "action_layer_absent"} \
            in body["degraded"]
        assert body["buckets"][0]["total"] == 1

    async def test_limit_is_echoed_and_clamped(
            self, admin_client, verdict_store):
        await _record(verdict_store)
        body = (await admin_client.get(
            "/v1/admin/decisions/quality?datasource=demo&limit=1")).json()
        assert body["limit"] == 1 and body["verdicts_read"] == 1
        body = (await admin_client.get(
            "/v1/admin/decisions/quality?datasource=demo&limit=99999")).json()
        assert body["limit"] == 5000

    async def test_quality_route_is_not_swallowed_by_the_rule_id_pattern(
            self, admin_client, decisions_app, verdict_store):
        """有一条 id 就叫 ``quality`` 的规则时,端点仍须命中质量报告
        (同 ``/raw``、``/verdicts/{id}`` 的 Starlette 顺序纪律)。"""
        _write(decisions_app, [{**RULE, "id": "quality"}])
        r = await admin_client.get("/v1/admin/decisions/quality?datasource=demo")
        assert r.status_code == 200, r.text
        assert "buckets" in r.json()

    async def test_without_a_verdict_store_the_endpoint_is_409(
            self, admin_client):
        """端点存在的意义就是读判定史;读不到就是 409,不装作空报告。"""
        assert (await admin_client.get(
            "/v1/admin/decisions/quality?datasource=demo")).status_code == 409

    async def test_a_corrupt_file_is_422_not_a_quality_report(
            self, admin_client, decisions_app, verdict_store):
        path = decisions_app.state.kb.decisions_path("demo")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("rules: [{id: a}, {id: a}]", encoding="utf-8")
        assert (await admin_client.get(
            "/v1/admin/decisions/quality?datasource=demo")).status_code == 422


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

    async def test_quality_report_is_admin_only(self, user_client):
        assert (await user_client.get(
            "/v1/admin/decisions/quality?datasource=demo")).status_code == 403

    async def test_requires_auth(self, decisions_app):
        transport = ASGITransport(app=decisions_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/v1/admin/decisions?datasource=demo")
            assert r.status_code == 401
