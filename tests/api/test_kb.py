"""Knowledge base / semantic model endpoint tests.

Uses the `api_kb` fixture: app whose KB is seeded with one datasource's
YAML files (terms/examples/lessons/rules/schema_notes), synced to the
SQLite mirror.
"""

from __future__ import annotations

import pytest
import yaml
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def kb_client(api_kb, admin_token):
    """Authenticated admin client bound to the seeded KB app."""
    transport = ASGITransport(app=api_kb)
    headers = {"Authorization": f"Bearer {admin_token}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


class TestKbStatus:
    async def test_status_enabled_with_counts(self, kb_client):
        resp = await kb_client.get("/v1/kb/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["items"]["test_db"]["term"] == 1
        assert body["items"]["test_db"]["example"] == 1
        assert body["items"]["test_db"]["lesson"] == 1

    async def test_rules(self, kb_client):
        resp = await kb_client.get("/v1/kb/rules")
        assert resp.status_code == 200
        assert any("千元" in r for r in resp.json()["rules"])


class TestKbStatusVisibility:
    """/kb/status 没有 datasource 参数 —— 它枚举全量,所以可见性在响应里过滤。

    任何登录用户能枚举出未授权数据源的名字本身就是一次泄露;过滤复用
    ``visible_datasources``(与 catalog 列表页同一份实现),不是另写一套。
    """

    @staticmethod
    def _seed_second_source(api_kb) -> None:
        """再种一个源(只有 rules.yml)进镜像,让"枚举"这件事有东西可枚举。"""
        ds_dir = api_kb.state.kb.kb_dir / "other_db"
        ds_dir.mkdir(parents=True, exist_ok=True)
        (ds_dir / "rules.yml").write_text(
            "rules:\n  - rule: 另一源的口径\n", encoding="utf-8",
        )

    async def test_admin_sees_all_users_see_visible(
        self, api_kb, client, user_client, auth_service,
    ):
        self._seed_second_source(api_kb)
        await api_kb.state.kb.ensure_synced("other_db")

        by_admin = (await client.get("/v1/kb/status")).json()
        assert {"test_db", "other_db"} <= set(by_admin["items"])
        assert by_admin["enabled"] is True

        # bob 空 grants → 只默认源(test_db 之外的名字一个都不出现)
        assert set((await user_client.get("/v1/kb/status")).json()["items"]) == {"test_db"}

        bob = await auth_service.authenticate("bob", "bobpw")
        # 非空 grants = 严格白名单:给了 other_db,默认源反而不可见
        await auth_service.set_datasources(bob["id"], ["other_db"])
        assert set((await user_client.get("/v1/kb/status")).json()["items"]) == {"other_db"}

        await auth_service.set_datasources(bob["id"], ["test_db", "other_db"])
        assert set((await user_client.get("/v1/kb/status")).json()["items"]) == {
            "test_db", "other_db",
        }


class TestTerms:
    async def test_search_terms(self, kb_client):
        resp = await kb_client.get("/v1/kb/terms", params={"q": "平均成绩"})
        assert resp.status_code == 200
        terms = resp.json()["terms"]
        assert terms and terms[0]["term"] == "平均成绩"
        assert terms[0]["mapping"] == "AVG(students.grade)"

    async def test_list_term_names(self, kb_client):
        resp = await kb_client.get("/v1/kb/terms")
        assert resp.status_code == 200
        names = [t["term"] for t in resp.json()["terms"]]
        assert "平均成绩" in names

    async def test_create_term_writes_yaml(self, kb_client, api_kb):
        resp = await kb_client.post("/v1/kb/terms", json={
            "term": "学生数",
            "mapping": "COUNT(students.id)",
            "tables": ["students"],
            "definition": "学生总人数",
        })
        assert resp.status_code == 201
        assert resp.json()["term"] == "学生数"

        kb = api_kb.state.kb
        data = yaml.safe_load((kb.kb_dir / "test_db" / "semantics.yml").read_text(encoding="utf-8"))
        metrics = data["semantic_model"][0]["metrics"]
        assert any(m["name"] == "学生数" for m in metrics)

    async def test_create_term_empty_422(self, kb_client):
        resp = await kb_client.post("/v1/kb/terms", json={"term": ""})
        assert resp.status_code == 422


class TestExamples:
    async def test_search_examples(self, kb_client):
        resp = await kb_client.get("/v1/kb/examples", params={"q": "平均成绩"})
        assert resp.status_code == 200
        examples = resp.json()["examples"]
        assert examples and examples[0]["question"] == "学生们的平均成绩是多少"

    async def test_list_example_questions(self, kb_client):
        resp = await kb_client.get("/v1/kb/examples")
        assert resp.status_code == 200
        questions = [e["question"] for e in resp.json()["examples"]]
        assert "学生们的平均成绩是多少" in questions

    async def test_create_example_writes_yaml(self, kb_client, api_kb):
        resp = await kb_client.post("/v1/kb/examples", json={
            "question": "每个地区的最高成绩是多少",
            "sql": "SELECT county, MAX(grade) FROM students GROUP BY county",
            "tags": ["成绩"],
        })
        assert resp.status_code == 201

        kb = api_kb.state.kb
        data = yaml.safe_load((kb.kb_dir / "test_db" / "examples.yml").read_text(encoding="utf-8"))
        assert any(e["question"].startswith("每个地区") for e in data["examples"])


class TestLessons:
    async def test_pending_excluded_by_default(self, kb_client):
        resp = await kb_client.get("/v1/kb/lessons")
        assert resp.status_code == 200
        assert resp.json()["lessons"] == []  # the seed lesson is pending

    async def test_pending_included_with_flag(self, kb_client):
        resp = await kb_client.get("/v1/kb/lessons", params={"pending": "true"})
        assert resp.status_code == 200
        lessons = resp.json()["lessons"]
        assert len(lessons) == 1
        assert lessons[0]["confirmed"] is False

    async def test_create_lesson_pending(self, kb_client):
        resp = await kb_client.post("/v1/kb/lessons", json={
            "pattern": "JOIN 键类型不一致",
            "note": "用 CAST 统一后再 JOIN",
        })
        assert resp.status_code == 201
        resp = await kb_client.get("/v1/kb/lessons", params={"pending": "true"})
        assert any(l["pattern"] == "JOIN 键类型不一致" for l in resp.json()["lessons"])

    async def test_confirm_lessons_rewrites_yaml(self, kb_client, api_kb):
        resp = await kb_client.post("/v1/kb/lessons/confirm")
        assert resp.status_code == 200
        assert resp.json()["confirmed"] == 1

        kb = api_kb.state.kb
        data = yaml.safe_load((kb.kb_dir / "test_db" / "lessons.yml").read_text(encoding="utf-8"))
        assert all(l["confirmed"] for l in data["lessons"])
        # confirmed lessons now visible without the pending flag
        resp = await kb_client.get("/v1/kb/lessons")
        assert len(resp.json()["lessons"]) == 1


class TestRatings:
    async def test_upvote_creates_pending_lesson(self, kb_client, api_kb):
        resp = await kb_client.post("/v1/kb/ratings", json={
            "question": "平均贷款金额是多少",
            "note": "答案",
            "sql_snippet": "SELECT AVG(amount) FROM loans",
            "vote": 1,
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["lesson"]["upvotes"] == 1
        assert body["lesson"]["downvotes"] == 0
        assert body["lesson"]["confirmed"] is False

        kb = api_kb.state.kb
        data = yaml.safe_load((kb.kb_dir / "test_db" / "lessons.yml").read_text(encoding="utf-8"))
        lesson = next(l for l in data["lessons"] if l.get("question") == "平均贷款金额是多少")
        assert lesson["upvotes"] == 1

    async def test_same_question_upserts_counts(self, kb_client):
        for _ in range(2):
            resp = await kb_client.post("/v1/kb/ratings", json={
                "question": "重复问题",
                "vote": 1,
            })
            assert resp.status_code == 201
        resp = await kb_client.post("/v1/kb/ratings", json={
            "question": "重复问题",
            "vote": -1,
        })
        assert resp.status_code == 201
        lesson = resp.json()["lesson"]
        assert lesson["upvotes"] == 2
        assert lesson["downvotes"] == 1

    async def test_invalid_vote_rejected(self, kb_client):
        resp = await kb_client.post("/v1/kb/ratings", json={
            "question": "测试",
            "vote": 5,
        })
        assert resp.status_code == 422

    async def test_rating_listed_in_pending(self, kb_client):
        await kb_client.post("/v1/kb/ratings", json={
            "question": "点踩问题",
            "vote": -1,
        })
        resp = await kb_client.get("/v1/kb/lessons", params={"pending": "true"})
        lessons = resp.json()["lessons"]
        rated = next(l for l in lessons if l.get("question") == "点踩问题")
        assert rated["downvotes"] == 1


class TestTableNotes:
    async def test_table_notes(self, kb_client):
        resp = await kb_client.get("/v1/kb/tables/students/notes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["description"] == "学生表"
        assert body["columns"]["grade"] == "成绩"

    async def test_table_notes_missing_404(self, kb_client):
        resp = await kb_client.get("/v1/kb/tables/nope/notes")
        assert resp.status_code == 404


class TestKbAssets:
    """GET /v1/kb/assets —— 资产来源与格式体检(C1,只读)。"""

    async def test_reports_seeded_assets(self, kb_client):
        resp = await kb_client.get("/v1/kb/assets?datasource=test_db")
        assert resp.status_code == 200
        body = resp.json()
        assert body["datasource"] == "test_db"
        assert body["refused"] == {}
        by_file = {a["file"]: a for a in body["assets"]}
        # 种子里是手写的 YAML(无 `_meta`)→ format 0,"改没改过"无从判断
        assert by_file["schema_notes.yml"]["format"] == 0
        assert by_file["schema_notes.yml"]["edited"] is None

    async def test_refused_asset_is_reported(self, kb_client, api_kb):
        """比代码新的资产:不进镜像,且必须在报告里点名。

        镜像里留着上一次读懂的样子 —— 没有这份报告,"KB 看起来正常"和
        "磁盘上的文件被采纳了"就分不开。
        """
        import yaml as _y

        from trove.services.kb.provenance import META_KEY, FORMAT_VERSION, dump_asset

        ds_dir = api_kb.state.kb.kb_dir / "test_db"
        doc = _y.safe_load((ds_dir / "schema_notes.yml").read_text(encoding="utf-8"))
        before = await api_kb.state.kb.list_items()

        future = dump_asset(doc, "trove-from-the-future")
        future = _y.safe_load(future)
        future[META_KEY]["format"] = FORMAT_VERSION + 5
        (ds_dir / "schema_notes.yml").write_text(
            _y.safe_dump(future, allow_unicode=True, sort_keys=False), encoding="utf-8",
        )
        await api_kb.state.kb.force_sync("test_db")

        resp = await kb_client.get("/v1/kb/assets?datasource=test_db")
        body = resp.json()
        assert "test_db/schema_notes.yml" in body["refused"]
        assert str(FORMAT_VERSION + 5) in body["refused"]["test_db/schema_notes.yml"]
        # 镜像没被换掉
        assert await api_kb.state.kb.list_items() == before


KB_READ_ENDPOINTS = [
    "/v1/kb/assets",
    "/v1/kb/rules",
    "/v1/kb/entries",
    "/v1/kb/terms",
    "/v1/kb/examples",
    "/v1/kb/lessons",
    "/v1/kb/tables/students/notes",
]


class TestKbReadGating:
    """KB 读端点与 catalog/lineage 同一把闸:grants 之外的数据源 → 403。

    读侧此前只挂 ``get_current_user`` —— 任何登录用户对任意数据源都能读到
    KB 内容(schema 注释、口径、few-shot 示例),而同一份数据源的
    ``/catalog/tables`` 是 403。闸门只装在一半入口上等于没装。
    """

    @pytest.fixture
    async def two_sources(self, api_kb, auth_service):
        """注册第二个源(未授权给 bob),bob 的 grants 收窄为 [test_db]。"""
        from trove.core.types import DatasourceConfig

        await api_kb.state.connector_registry.register(DatasourceConfig(
            name="extra", type="sqlite",
            connection_params={"path": ":memory:"}, credentials={}, default=False,
        ))
        bob = await auth_service.authenticate("bob", "bobpw")
        await auth_service.set_datasources(bob["id"], ["test_db"])
        return api_kb

    @pytest.mark.parametrize("path", KB_READ_ENDPOINTS)
    async def test_user_refused_for_ungranted_source(
        self, path, user_client, two_sources,
    ):
        resp = await user_client.get(path, params={"datasource": "extra"})
        assert resp.status_code == 403, resp.text
        assert "extra" in resp.json()["detail"]

    @pytest.mark.parametrize("path", KB_READ_ENDPOINTS)
    async def test_granted_source_still_reads(self, path, user_client, two_sources):
        resp = await user_client.get(path, params={"datasource": "test_db"})
        assert resp.status_code == 200, resp.text

    async def test_default_source_needs_no_grant(self, user_client, api_kb):
        """空 grants → 默认源(单数据源部署不需要预先配 grant)。"""
        resp = await user_client.get("/v1/kb/terms")
        assert resp.status_code == 200

    async def test_admin_reads_any_source(self, client, two_sources):
        resp = await client.get("/v1/kb/terms", params={"datasource": "extra"})
        assert resp.status_code == 200
        assert resp.json()["terms"] == []


class TestRatingsPromotion:
    """点赞 → upvote 证据。仅在 memory.promotion 开启时生效(默认关)。"""

    @pytest.fixture
    async def promoted_client(self, api_kb, admin_token, tmp_path):
        """给 app 挂一个 promotion 打开的 MemoryService(生产装配见 main.py)。"""
        from trove.services.memory.models import MemoryConfig
        from trove.services.memory.service import MemoryService

        api_kb.state.memory = MemoryService(
            tmp_path / "mem",
            MemoryConfig(enabled=True, promotion=True, promotion_threshold=0.8),
            kb=api_kb.state.kb,
        )
        transport = ASGITransport(app=api_kb)
        headers = {"Authorization": f"Bearer {admin_token}"}
        async with AsyncClient(
            transport=transport, base_url="http://test", headers=headers
        ) as c:
            yield c

    @staticmethod
    def _lesson(api_kb, question):
        kb_dir = api_kb.state.kb.kb_dir / "test_db"
        data = yaml.safe_load((kb_dir / "lessons.yml").read_text(encoding="utf-8"))
        return next(l for l in data["lessons"] if l.get("question") == question)

    async def test_upvotes_accumulate_and_promote(self, promoted_client, api_kb):
        for _ in range(2):
            resp = await promoted_client.post("/v1/kb/ratings", json={
                "question": "平均贷款金额是多少",
                "vote": 1,
                "sql_snippet": "SELECT AVG(amount) FROM loans",
            })
            assert resp.status_code == 201

        lesson = self._lesson(api_kb, "平均贷款金额是多少")
        assert lesson["confidence"] == 0.8
        assert lesson["confirmed"] is True

    async def test_single_upvote_stays_pending(self, promoted_client, api_kb):
        resp = await promoted_client.post("/v1/kb/ratings", json={
            "question": "只点一次的问题", "vote": 1,
        })
        assert resp.status_code == 201

        lesson = self._lesson(api_kb, "只点一次的问题")
        assert lesson["confidence"] == 0.4
        assert lesson["confirmed"] is False

    async def test_inert_when_promotion_absent(self, kb_client, api_kb):
        """没有 memory 组件 = 未开启 → 一个字节都不该变。"""
        for _ in range(2):
            resp = await kb_client.post("/v1/kb/ratings", json={
                "question": "不动问题", "vote": 1,
            })
            assert resp.status_code == 201

        lesson = self._lesson(api_kb, "不动问题")
        assert "confidence" not in lesson
        assert lesson["confirmed"] is False
