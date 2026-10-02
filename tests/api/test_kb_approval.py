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


class TestQueueActionEndpoints:
    """待审批队列的动作端点(逐条确认/拒绝、编辑后确认、审计一致性)。

    这一族是管理台新队列的硬前置:键走 body(pattern 含 ``/`` 时 ``%2F``
    在 path 参数上没有保证)、键两认(pattern / question)、逐条认证门、
    每次动作落审计。旧 path 路由保留(admin 路由),这里只测新增的。
    """

    async def test_confirm_one_by_question_key(self, api_kb, user_client, client):
        """投票产生的 lesson 只有 question —— 旧接口对它必然 404,新接口两认。"""
        await user_client.post("/v1/kb/ratings", json={
            "vote": 1, "question": "哪个地区贷款总额最高", "note": "投票条目",
        })
        resp = await client.post("/v1/kb/lessons/confirm-one", json={
            "key": "哪个地区贷款总额最高", "datasource": "test_db",
        })
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "confirmed", "key": "哪个地区贷款总额最高",
            "audit": "kb.lesson.confirm",
        }
        from trove.services.kb.service import yaml
        data = yaml.safe_load(
            (api_kb.state.kb.kb_dir / "test_db" / "lessons.yml").read_text(encoding="utf-8"))
        entry = next(l for l in data["lessons"] if l.get("question") == "哪个地区贷款总额最高")
        assert entry["confirmed"] is True
        # 投票产生的条目来源如实(镜像缺省 "manual" 会显示错值)
        assert entry["source"] == "user_feedback"

    async def test_confirm_one_with_note_override(self, api_kb, client):
        """编辑后确认:note 先落地再确认,盘上能看到新 note。"""
        await client.post("/v1/kb/lessons", json={"pattern": "改后确认", "note": "旧"})
        resp = await client.post("/v1/kb/lessons/confirm-one", json={
            "key": "改后确认", "note": "改过的说明", "datasource": "test_db",
        })
        assert resp.status_code == 200
        from trove.services.kb.service import yaml
        data = yaml.safe_load(
            (api_kb.state.kb.kb_dir / "test_db" / "lessons.yml").read_text(encoding="utf-8"))
        entry = next(l for l in data["lessons"] if l["pattern"] == "改后确认")
        assert entry["note"] == "改过的说明"
        assert entry["confirmed"] is True

    async def test_reject_one_by_key_and_404(self, api_kb, client):
        await client.post("/v1/kb/lessons", json={"pattern": "待删", "note": "x"})
        resp = await client.post("/v1/kb/lessons/reject-one", json={
            "key": "待删", "datasource": "test_db",
        })
        assert resp.status_code == 200
        assert resp.json()["audit"] == "kb.lesson.reject"
        assert (await client.post("/v1/kb/lessons/reject-one", json={
            "key": "不存在的键", "datasource": "test_db",
        })).status_code == 404

    async def test_queue_actions_are_admin_only(self, user_client):
        for path in ("/v1/kb/lessons/confirm-one", "/v1/kb/lessons/reject-one"):
            assert (await user_client.post(path, json={"key": "x"})).status_code == 403
        for path in ("/v1/kb/examples/confirm-one", "/v1/kb/examples/reject-one"):
            assert (await user_client.post(
                path, json={"question": "q", "sql": "SELECT 1"})).status_code == 403

    async def test_one_endpoint_is_audited(self, api_kb, client, auth_service):
        await client.post("/v1/kb/lessons", json={"pattern": "审计项", "note": "x"})
        await client.post("/v1/kb/lessons/confirm-one", json={
            "key": "审计项", "datasource": "test_db",
        })
        entries = await auth_service.list_audit(action="kb.lesson.confirm")
        assert entries
        assert entries[0]["details"]["key"] == "审计项"

    async def test_batch_confirm_is_audited_too(self, api_kb, client, auth_service):
        """批量确认和单条一条账(此前全量确认不写审计)。"""
        resp = await client.post("/v1/kb/lessons/confirm")
        assert resp.status_code == 200
        entries = await auth_service.list_audit(action="kb.lesson.confirm")
        assert entries
        assert entries[0]["details"] == {
            "datasource": "test_db", "all": True, "confirmed": 1,
        }


class TestPerExampleApproval:
    """逐条示例确认/拒绝:question+sql 定位、逐条认证门、审计。"""

    async def _draft(self, user_client, question="东区贷款总额是多少",
                     sql="SELECT SUM(amount) FROM loan WHERE district='East'"):
        resp = await user_client.post("/v1/kb/examples/draft", json={
            "question": question, "sql": sql,
        })
        assert resp.status_code == 201
        return question, sql

    async def test_confirm_one_certifies_with_actor(self, api_kb, user_client, client):
        q, sql = await self._draft(user_client)
        resp = await client.post("/v1/kb/examples/confirm-one", json={
            "question": q, "sql": sql, "datasource": "test_db",
        })
        assert resp.status_code == 200
        assert resp.json()["audit"] == "kb.example.confirm"
        from trove.services.kb.service import yaml
        doc = yaml.safe_load(
            (api_kb.state.kb.kb_dir / "test_db" / "examples.yml").read_text(encoding="utf-8"))
        drafted = next(ex for ex in doc["examples"] if ex["question"] == q)
        assert "pending" not in drafted
        assert drafted["governance"]["approved_by"] == "admin"

    async def test_bad_sql_is_refused_per_item_and_others_survive(
        self, api_kb, user_client, client,
    ):
        """逐条认证门:一条坏 SQL 409 并留在 pending,不拖住别的好资产。"""
        good_q, good_sql = await self._draft(user_client, question="好条目")
        await self._draft(user_client, question="坏条目", sql="DELETE FROM students")

        bad = await client.post("/v1/kb/examples/confirm-one", json={
            "question": "坏条目", "sql": "DELETE FROM students", "datasource": "test_db",
        })
        assert bad.status_code == 409
        assert "坏条目" in bad.json()["detail"]

        good = await client.post("/v1/kb/examples/confirm-one", json={
            "question": good_q, "sql": good_sql, "datasource": "test_db",
        })
        assert good.status_code == 200
        # 坏条目仍留在 pending(待 admin 改 SQL 后重试)
        pending = (await client.get("/v1/kb/examples/pending")).json()["examples"]
        assert [ex["question"] for ex in pending] == ["坏条目"]

    async def test_reject_one_removes_only_that_draft(self, api_kb, user_client, client):
        q1, s1 = await self._draft(user_client, question="留下")
        q2, s2 = await self._draft(user_client, question="丢掉")
        resp = await client.post("/v1/kb/examples/reject-one", json={
            "question": q2, "sql": s2, "datasource": "test_db",
        })
        assert resp.status_code == 200
        pending = (await client.get("/v1/kb/examples/pending")).json()["examples"]
        assert [ex["question"] for ex in pending] == [q1]
        # 再拒一次 → 404(条目已不在)
        assert (await client.post("/v1/kb/examples/reject-one", json={
            "question": q2, "sql": s2, "datasource": "test_db",
        })).status_code == 404

    async def test_draft_carries_created_at(self, api_kb, user_client, client):
        """草稿带 created_at:队列「躺了多久 / 按时间排序」的前提。"""
        await self._draft(user_client, question="带时间戳的草稿")
        pending = (await client.get("/v1/kb/examples/pending")).json()["examples"]
        entry = next(ex for ex in pending if ex["question"] == "带时间戳的草稿")
        assert entry["created_at"]


class TestSemanticEntriesEndpoint:
    async def test_entries_carry_kind_and_item_key(self, api_kb, client):
        resp = await client.get("/v1/kb/entries")
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        kinds = [e["kind"] for e in entries]
        # term 与 metric 是同一份 payload 的两套投影 —— 只出 metric 一套
        assert kinds.count("metric") == 1
        assert "term" not in kinds
        metric = next(e for e in entries if e["kind"] == "metric")
        assert metric["key"] == "平均成绩"
        assert metric["expression"]
        table = next(e for e in entries if e["kind"] == "table")
        assert table["key"] == "students"
        assert table["description"] == "学生表"

    async def test_lesson_payload_carries_timestamps(self, api_kb, client):
        """镜像 lesson payload 透传 created_at / updated_at(时间列的前提)。"""
        await client.post("/v1/kb/lessons", json={"pattern": "带时间", "note": "x"})
        lessons = (await client.get("/v1/kb/lessons?pending=true")).json()["lessons"]
        entry = next(l for l in lessons if l["pattern"] == "带时间")
        assert entry["created_at"]  # append 路径写 created_at
        assert "updated_at" in entry
