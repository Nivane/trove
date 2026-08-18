"""Chat + session endpoint tests (SSE over the real reflection graph)."""

from __future__ import annotations

import json


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse a text/event-stream body into [(event, data), ...]."""
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        etype = lines[0].split(":", 1)[1].strip()
        data = json.loads(lines[1].split(":", 1)[1])
        events.append((etype, data))
    return events


class TestSessions:
    async def test_create_session(self, client):
        resp = await client.post("/v1/sessions")
        assert resp.status_code == 201
        assert resp.json()["session_id"]

    async def test_create_and_get_session(self, client):
        created = (await client.post("/v1/sessions")).json()["session_id"]
        resp = await client.get(f"/v1/sessions/{created}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == created
        assert body["messages"] == []

    async def test_get_missing_session_404(self, client):
        resp = await client.get("/v1/sessions/nope")
        assert resp.status_code == 404

    async def test_delete_session(self, client):
        created = (await client.post("/v1/sessions")).json()["session_id"]
        assert (await client.delete(f"/v1/sessions/{created}")).status_code == 204
        assert (await client.get(f"/v1/sessions/{created}")).status_code == 404
        assert (await client.delete(f"/v1/sessions/{created}")).status_code == 404

    async def test_list_sessions(self, client):
        assert (await client.post("/v1/sessions")).status_code == 201
        resp = await client.get("/v1/sessions")
        assert resp.status_code == 200
        assert isinstance(resp.json()["sessions"], list)


class TestChat:
    async def test_chat_streams_typed_events(self, client):
        resp = await client.post(
            "/v1/chat",
            json={"question": "What students are in Alameda county?"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        events = parse_sse(resp.text)
        types = [t for t, _ in events]
        assert types[0] == "session"
        assert types[-1] == "done"
        assert "sql" in types
        assert "result" in types
        # the session event teaches the client the session id
        session_id = events[0][1]["session_id"]
        assert session_id
        assert events[-1][1]["summary"]["sql"] == "SELECT name FROM students;"

    async def test_chat_existing_session_continues(self, client):
        created = (await client.post("/v1/sessions")).json()["session_id"]
        resp = await client.post(
            "/v1/chat",
            json={"session_id": created, "question": "Which county has most students?"},
        )
        events = parse_sse(resp.text)
        assert events[0][1]["session_id"] == created

        detail = (await client.get(f"/v1/sessions/{created}")).json()
        assert len(detail["messages"]) == 2  # user + assistant recorded

    async def test_chat_missing_session_404(self, client):
        resp = await client.post(
            "/v1/chat",
            json={"session_id": "nope", "question": "hello"},
        )
        assert resp.status_code == 404

    async def test_chat_unknown_workflow_emits_error_event(self, client):
        resp = await client.post(
            "/v1/chat",
            json={"question": "hi", "workflow": "nonexistent"},
        )
        events = parse_sse(resp.text)
        assert events[-1][0] == "error"

    async def test_chat_empty_question_422(self, client):
        resp = await client.post("/v1/chat", json={"question": ""})
        assert resp.status_code == 422

    async def test_health(self, client):
        resp = await client.get("/v1/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
