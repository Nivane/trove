"""KB feedback loop over the API: users submit pending lessons, admins confirm/reject per-item."""

from __future__ import annotations


class TestUserFeedbackChannel:
    async def test_user_can_submit_pending_lesson(self, user_client, api_app):
        resp = await user_client.post("/v1/kb/lessons", json={
            "pattern": "用户反馈:金额单位混用",
            "note": "金额列同时存在元和千元口径",
        })
        assert resp.status_code == 201
        lessons = (await user_client.get("/v1/kb/lessons?pending=true")).json()["lessons"]
        assert any(l["pattern"] == "用户反馈:金额单位混用" and not l["confirmed"] for l in lessons)
        # confirmed view excludes it
        confirmed = (await user_client.get("/v1/kb/lessons")).json()["lessons"]
        assert all(l["pattern"] != "用户反馈:金额单位混用" for l in confirmed)

    async def test_user_cannot_write_terms_or_examples(self, user_client):
        assert (await user_client.post(
            "/v1/kb/terms",
            json={"term": "x", "mapping": "AVG(students.grade)", "tables": ["students"]},
        )).status_code == 403
        assert (await user_client.post(
            "/v1/kb/examples", json={"question": "q", "sql": "SELECT 1"}
        )).status_code == 403
        assert (await user_client.post(
            "/v1/kb/lessons/confirm"
        )).status_code == 403

    async def test_anonymous_cannot_submit(self, anon_client):
        resp = await anon_client.post("/v1/kb/lessons", json={"pattern": "x", "note": "y"})
        assert resp.status_code == 401


class TestAdminPerLessonApproval:
    async def test_confirm_one_lesson(self, api_kb, user_client, client):
        # api_kb seeds lessons.yml with one pending lesson (KB_SEED)
        pending = (await user_client.get("/v1/kb/lessons?pending=true")).json()["lessons"]
        assert len(pending) == 1
        pattern = pending[0]["pattern"]

        resp = await client.post(f"/v1/admin/kb/lessons/{pattern}/confirm")
        assert resp.status_code == 200
        assert resp.json()["confirmed"] is True

        # now confirmed: visible in the default (confirmed) view;
        # the pending=true view is an unfiltered superset — the entry
        # must now carry confirmed=True there too
        confirmed = (await client.get("/v1/kb/lessons")).json()["lessons"]
        assert any(l["pattern"] == pattern and l["confirmed"] for l in confirmed)
        pending_after = (await client.get("/v1/kb/lessons?pending=true")).json()["lessons"]
        entry = next(l for l in pending_after if l["pattern"] == pattern)
        assert entry["confirmed"] is True

        # YAML rewritten on disk
        kb = api_kb.state.kb
        from trove.services.kb.service import yaml
        data = yaml.safe_load((kb.kb_dir / "test_db" / "lessons.yml").read_text(encoding="utf-8"))
        seeded = next(l for l in data["lessons"] if l["pattern"] == pattern)
        assert seeded["confirmed"] is True

    async def test_confirm_unknown_pattern_404(self, client):
        resp = await client.post("/v1/admin/kb/lessons/不存在的模式/confirm")
        assert resp.status_code == 404

    async def test_reject_removes_lesson(self, api_kb, user_client, client):
        kb = api_kb.state.kb
        # Add a second pending lesson to reject
        await user_client.post("/v1/kb/lessons", json={"pattern": "待驳回", "note": "噪声"})
        resp = await client.post("/v1/admin/kb/lessons/待驳回/reject")
        assert resp.status_code == 200
        assert resp.json()["rejected"] is True

        pending = (await client.get("/v1/kb/lessons?pending=true")).json()["lessons"]
        assert all(l["pattern"] != "待驳回" for l in pending)
        # gone from YAML too
        from trove.services.kb.service import yaml
        data = yaml.safe_load((kb.kb_dir / "test_db" / "lessons.yml").read_text(encoding="utf-8"))
        assert all(l["pattern"] != "待驳回" for l in data["lessons"])

    async def test_reject_unknown_404(self, client):
        resp = await client.post("/v1/admin/kb/lessons/nope/reject")
        assert resp.status_code == 404

    async def test_non_admin_cannot_approve(self, user_client):
        assert (await user_client.post("/v1/admin/kb/lessons/x/confirm")).status_code == 403
        assert (await user_client.post("/v1/admin/kb/lessons/x/reject")).status_code == 403

    async def test_approval_audited(self, api_kb, client, auth_service):
        pending = (await client.get("/v1/kb/lessons?pending=true")).json()["lessons"]
        await client.post(f"/v1/admin/kb/lessons/{pending[0]['pattern']}/confirm")
        entries = await auth_service.list_audit(action="kb.lesson.confirm")
        assert len(entries) == 1
        assert entries[0]["username"] == "admin"
