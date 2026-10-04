"""预设包管理端 API —— list/get/apply/put/history/rollback + 规则草稿队列。

红线回归(HTTP 面):``apply`` 走完一遍之后,``decisions.yml`` **不存在**、
org skills 里那份技能是 ``pending``;规则只在 ``decision_drafts.yml`` 的
待审队列里,经 ``/admin/decisions/drafts/{id}/confirm`` 确认后才落进
``decisions.yml``。整轮零 LLM / 零网络。
"""

from __future__ import annotations

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from trove.api.app import create_app
from trove.core.config import AgentConfig
from trove.llm.gateway import LLMGateway
from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.config_store import ConfigStore
from trove.services.kb.service import KbService
from trove.services.presets.service import PresetService
from trove.services.skills.service import SkillService

from tests.helpers.kb import ossie_semantics_yaml

KB_SEED = ossie_semantics_yaml([{
    "term": "平均成绩",
    "aliases": ["均分"],
    "mapping": "AVG(students.grade)",
    "tables": ["students"],
    "definition": "学生平均分",
}])


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
async def presets_app(sqlite_registry, tmp_path, auth_service):
    kb = KbService(tmp_path / "proj")
    kb.kb_dir.mkdir(parents=True)
    await kb.ensure_synced(None)
    ds_dir = kb.kb_dir / "test_db"
    ds_dir.mkdir(parents=True, exist_ok=True)
    (ds_dir / "semantics.yml").write_text(KB_SEED, encoding="utf-8")
    skills = SkillService(tmp_path / "proj" / ".trove" / "skills")
    presets = PresetService(
        tmp_path / "proj" / ".trove" / "presets",
        kb=kb, skills=skills, git_enabled=False)
    return create_app({
        "session_manager": None,
        "catalog_service": CatalogService(sqlite_registry),
        "connector_registry": sqlite_registry,
        "kb": kb,
        "auth": auth_service,
        "config_store": ConfigStore(tmp_path / "proj" / ".trove" / "datasources.yml"),
        "llm_gateway": LLMGateway(mock_response="x"),
        "config": AgentConfig(target="mock/model"),
        "skills": skills,
        "presets": presets,
    })


@pytest.fixture
async def client(presets_app, admin_token):
    transport = ASGITransport(app=presets_app)
    headers = {"Authorization": f"Bearer {admin_token}"}
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers=headers) as c:
        yield c


@pytest.fixture
async def user_client(presets_app, user_token):
    transport = ASGITransport(app=presets_app)
    headers = {"Authorization": f"Bearer {user_token}"}
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers=headers) as c:
        yield c


ORG_PRESET = {
    "name": "org-starter",
    "description": "组织接入模板",
    "skills": [{
        "name": "org-caliber",
        "description": "Across-source caliber.",
        "tier": "available",
        "body": "1. State the base.\n",
    }],
    "decisions": [{
        "id": "org-watch",
        "window": "last month",
        "subject": {"metrics": ["avg_grade"]},
        "baseline": {"kind": "prev_period"},
        "conditions": ["delta_pct > 0.2"],
    }],
}


class TestListAndGet:
    async def test_list_includes_builtin(self, client):
        body = (await client.get("/v1/admin/presets")).json()
        rows = {r["name"]: r for r in body["presets"]}
        assert "financial-analysis" in rows
        assert rows["financial-analysis"]["source"] == "builtin"
        assert rows["financial-analysis"]["counts"]["skills"] == 2

    async def test_get_returns_contract_summary_and_text(self, client):
        body = (await client.get("/v1/admin/presets/financial-analysis")).json()
        assert body["name"] == "financial-analysis"
        assert body["counts"]["decisions"] == 2
        assert "financial-analysis" in body["text"]

    async def test_unknown_is_404(self, client):
        r = await client.get("/v1/admin/presets/nope")
        assert r.status_code == 404

    async def test_admin_only(self, user_client):
        assert (await user_client.get("/v1/admin/presets")).status_code == 403


class TestPut:
    async def test_put_creates_org_preset(self, client):
        r = await client.put("/v1/admin/presets/org-starter",
                             json={"preset": ORG_PRESET})
        assert r.status_code == 200
        assert r.json()["source"] == "org"
        got = (await client.get("/v1/admin/presets/org-starter")).json()
        assert got["source"] == "org" and got["counts"]["skills"] == 1

    async def test_put_rejects_unknown_key(self, client):
        bad = {**ORG_PRESET, "name": "bad", "skillz": []}
        r = await client.put("/v1/admin/presets/bad", json={"preset": bad})
        assert r.status_code == 400
        assert "skillz" in r.json()["detail"]

    async def test_put_needs_exactly_one_payload(self, client):
        r = await client.put("/v1/admin/presets/x",
                             json={"preset": ORG_PRESET,
                                   "text": "name: x"})
        assert r.status_code == 400

    async def test_put_accepts_raw_text(self, client):
        text = yaml.safe_dump({**ORG_PRESET, "name": "raw-one"},
                              allow_unicode=True)
        r = await client.put("/v1/admin/presets/raw-one", json={"text": text})
        assert r.status_code == 200
        assert r.json()["name"] == "raw-one"

    async def test_non_admin_forbidden(self, user_client):
        r = await user_client.put("/v1/admin/presets/org-starter",
                                  json={"preset": ORG_PRESET})
        assert r.status_code == 403


class TestHistoryAndRollback:
    async def test_history_of_builtin_is_empty_not_error(self, client):
        r = await client.get("/v1/admin/presets/financial-analysis/history")
        assert r.status_code == 200 and r.json()["history"] == []

    async def test_rollback_without_org_copy_is_404(self, client):
        r = await client.post("/v1/admin/presets/financial-analysis/rollback",
                              json={"sha": "deadbeef"})
        assert r.status_code == 404


class TestApply:
    async def test_apply_lands_pending_only(self, client, presets_app):
        """回归门:HTTP 面套用之后,生效面一个字节都没动。"""
        await client.put("/v1/admin/presets/org-starter",
                         json={"preset": ORG_PRESET})
        r = await client.post("/v1/admin/presets/org-starter/apply",
                              json={"datasource": "test_db"})
        assert r.status_code == 200
        body = r.json()
        statuses = {(i["section"], i["item"]): i["status"] for i in body["items"]}
        assert statuses[("skills", "org-caliber")] == "drafted"
        assert statuses[("decisions", "org-watch")] == "drafted"

        kb = presets_app.state.kb
        assert kb.load_decisions("test_db").rules == []       # 执行面读不到
        assert not kb.decisions_path("test_db").exists()
        assert (kb.kb_dir / "test_db" / "decision_drafts.yml").exists()
        skill = presets_app.state.skills.read_skill("org-caliber")
        assert skill["status"] == "pending"                   # 投递面读不到

    async def test_apply_unknown_preset_is_404(self, client):
        r = await client.post("/v1/admin/presets/nope/apply",
                              json={"datasource": "test_db"})
        assert r.status_code == 404

    async def test_non_admin_forbidden(self, user_client):
        r = await user_client.post(
            "/v1/admin/presets/financial-analysis/apply",
            json={"datasource": "test_db"})
        assert r.status_code == 403


class TestDecisionDraftQueue:
    async def test_drafts_route_is_not_shadowed_by_rule_id(self, client):
        """路由顺序回归:``/drafts`` 与 ``/{rule_id}`` 同为三段,声明顺序错
        了就会被规则 id 吞掉(404) —— 这条钉死它。"""
        r = await client.get("/v1/admin/decisions/drafts?datasource=test_db")
        assert r.status_code == 200
        assert set(r.json()["drafts"]) == {"pending", "applied", "rejected"}

    async def test_apply_then_confirm_via_api(self, client, presets_app):
        await client.put("/v1/admin/presets/org-starter",
                         json={"preset": ORG_PRESET})
        await client.post("/v1/admin/presets/org-starter/apply",
                          json={"datasource": "test_db"})

        queue = (await client.get(
            "/v1/admin/decisions/drafts?datasource=test_db")).json()["drafts"]
        draft = queue["pending"][0]
        assert draft["rule"]["id"] == "org-watch"
        assert (draft["rule"] or {}).get("enabled") is False   # 安全默认

        # 规则列表把草稿与生效规则分开呈现
        rules = (await client.get(
            "/v1/admin/decisions?datasource=test_db")).json()
        assert rules["rules"] == []
        assert [d["rule_id"] for d in rules["pending_drafts"]] == ["org-watch"]

        r = await client.post(
            f"/v1/admin/decisions/drafts/{draft['id']}/confirm"
            "?datasource=test_db")
        assert r.status_code == 200 and r.json()["status"] == "applied"
        assert [x.id for x in presets_app.state.kb.load_decisions(
            "test_db").rules] == ["org-watch"]

    async def test_reject_leaves_decisions_untouched(self, client, presets_app):
        await client.put("/v1/admin/presets/org-starter",
                         json={"preset": ORG_PRESET})
        await client.post("/v1/admin/presets/org-starter/apply",
                          json={"datasource": "test_db"})
        draft = (await client.get(
            "/v1/admin/decisions/drafts?datasource=test_db")).json()[
                "drafts"]["pending"][0]
        r = await client.post(
            f"/v1/admin/decisions/drafts/{draft['id']}/reject"
            "?datasource=test_db")
        assert r.status_code == 200 and r.json()["status"] == "rejected"
        assert presets_app.state.kb.load_decisions("test_db").rules == []
        assert not presets_app.state.kb.decisions_path("test_db").exists()

    async def test_unknown_draft_is_404(self, client):
        r = await client.post(
            "/v1/admin/decisions/drafts/nope/confirm?datasource=test_db")
        assert r.status_code == 404
