"""Admin API for org skill assets: draft→confirm gate, tiers, load_skill."""

from __future__ import annotations

from trove.services.skills.service import SkillService


def _install_skills(api_app, tmp_path) -> SkillService:
    svc = SkillService(root=tmp_path / "proj" / ".trove" / "skills")
    api_app.state.skills = svc
    return svc


def _draft_payload(**overrides):
    payload = {
        "name": "recon-caliber",
        "description": "对账口径",
        "triggers": {"node": "query_sketch"},
        "tier": "available",
        "body": "1. diff 行级\n2. 对总额\n",
    }
    payload.update(overrides)
    return payload


async def test_admin_draft_list_confirm_body(api_app, tmp_path, client):
    svc = _install_skills(api_app, tmp_path)

    r = await client.post("/v1/admin/skills/draft", json=_draft_payload())
    assert r.status_code == 201
    assert r.json()["status"] == "pending"

    # pending 在列表中,且不可被 load_skill(门禁)
    r = await client.get("/v1/admin/skills")
    assert r.status_code == 200
    org = [s for s in r.json()["skills"] if s["name"] == "recon-caliber"]
    assert len(org) == 1 and org[0]["status"] == "pending"

    body = await client.get("/v1/admin/skills/recon-caliber/body")
    assert body.status_code == 200
    assert "对总额" in body.json()["body"]

    r = await client.post("/v1/admin/skills/recon-caliber/confirm")
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    assert svc.list_org()[0]["status"] == "confirmed"


async def test_admin_reject_deletes(api_app, tmp_path, client):
    _install_skills(api_app, tmp_path)
    await client.post("/v1/admin/skills/draft", json=_draft_payload())
    r = await client.post("/v1/admin/skills/recon-caliber/reject")
    assert r.status_code == 200
    r = await client.get("/v1/admin/skills")
    assert "recon-caliber" not in {s["name"] for s in r.json()["skills"]}


async def test_admin_set_tier(api_app, tmp_path, client):
    svc = _install_skills(api_app, tmp_path)
    await client.post("/v1/admin/skills/draft", json=_draft_payload())
    r = await client.post("/v1/admin/skills/recon-caliber/tier", json={"tier": "required"})
    assert r.status_code == 200
    assert r.json()["tier"] == "required"
    assert svc.list_org()[0]["tier"] == "required"


async def test_admin_draft_validation(api_app, tmp_path, client):
    _install_skills(api_app, tmp_path)
    # 空 body 被 schema min_length 拦截(422);非法名到达服务层(400)
    r = await client.post("/v1/admin/skills/draft", json=_draft_payload(body=""))
    assert r.status_code == 422
    r = await client.post("/v1/admin/skills/draft", json=_draft_payload(name="Bad Name!"))
    assert r.status_code == 400


async def test_skills_require_admin(api_app, tmp_path, user_client, anon_client):
    _install_skills(api_app, tmp_path)
    r = await user_client.get("/v1/admin/skills")
    assert r.status_code == 403
    r = await anon_client.get("/v1/admin/skills")
    assert r.status_code in (401, 403)


async def test_llm_draft_endpoint(api_app, tmp_path, client):
    """LLM 草稿端点:mock 网关产出正文 → pending 待确认。"""
    svc = SkillService(root=tmp_path / "proj" / ".trove" / "skills", llm=api_app.state.llm_gateway)
    api_app.state.skills = svc
    r = await client.post("/v1/admin/skills/llm-draft", json={
        "name": "loan-caliber",
        "description": "贷款口径",
        "node": "query_sketch",
        "purpose": "按行业分组的贷款统计口径",
        "lang": "zh",
    })
    assert r.status_code == 201
    payload = r.json()
    assert payload["status"] == "pending"
    assert len(payload["skill"]["body"]) > 0
    # 未确认前 load_skill 不可用
    assert svc.load_skill_content("loan-caliber", "zh").startswith("Skill 'loan-caliber' is not confirmed yet")
