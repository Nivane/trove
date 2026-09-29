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


class TestAdminExampleCertification:
    """示例确认要**留下批准人**,且坏文件要**拒绝**而不是 500。

    两条都是"确认"这个动作的契约,而不是加载侧的:治理字段怎么读由
    ``test_kb_service.py`` 管,这里管的是**走 API 走一遍能不能留下记录**。

    ``actor`` 以前没有从 router 传下去(函数签名有这个参数,没人给),于是
    接口上一按"确认",资产只是清了 pending、**没有认证记录** —— 整个认证
    能力从 API 走不到。这类"参数齐全但没人传"的缺口在单测里看不出来:
    service 层的用例都是自己传 ``actor=`` 的。
    """

    async def _draft_one(self, user_client):
        resp = await user_client.post("/v1/kb/examples/draft", json={
            "question": "东区贷款总额是多少",
            "sql": "SELECT SUM(amount) FROM loan WHERE district='East'",
        })
        assert resp.status_code == 201
        assert resp.json()["status"] == "drafted"

    async def test_confirm_certifies_with_the_approving_admin(
        self, api_kb, user_client, client,
    ):
        await self._draft_one(user_client)
        assert (await client.post("/v1/kb/examples/confirm")).status_code == 200

        from trove.services.kb.service import yaml
        path = api_kb.state.kb.kb_dir / "test_db" / "examples.yml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        drafted = next(
            ex for ex in doc["examples"]
            if ex["question"] == "东区贷款总额是多少"
        )
        assert "pending" not in drafted
        gov = drafted["governance"]
        assert gov["status"] == "certified"
        assert gov["approved_by"] == "admin"
        assert gov["approved_at"]

    async def test_confirm_refuses_a_corrupt_file_with_409(
        self, api_kb, user_client, client,
    ):
        """读不通的 ``examples.yml`` → 409,不是 500,更不是 200。

        500 说"我们坏了"(运维问题),而真相是"这个请求按当前文件状态不能
        执行"(调用方问题),且报错本身要能被人拿去做动作。200 更坏 ——
        它会真的把资产库覆盖掉(见 service 侧同名测试)。
        """
        await self._draft_one(user_client)
        path = api_kb.state.kb.kb_dir / "test_db" / "examples.yml"
        broke = "examples:\n\t- question: 坏文件\n"
        path.write_text(broke, encoding="utf-8")

        resp = await client.post("/v1/kb/examples/confirm")
        assert resp.status_code == 409
        assert path.read_text(encoding="utf-8") == broke

    async def test_a_refused_certification_is_409_too(self, api_kb, user_client, client):
        """认证门拒绝(坏 SQL)→ 同样是 409,且带上**哪一条**不过。

        admin 要拿这条信息去改 SQL;只说"失败了"等于让他自己去找 622 条里
        是哪一条。
        """
        await user_client.post("/v1/kb/examples/draft", json={
            "question": "把学生删掉",
            "sql": "DELETE FROM students",
        })
        resp = await client.post("/v1/kb/examples/confirm")
        assert resp.status_code == 409
        assert "把学生删掉" in resp.json()["detail"]


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
