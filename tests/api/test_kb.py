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
