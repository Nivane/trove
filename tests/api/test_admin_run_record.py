"""Admin-only request-record read endpoint.

Two laws: the record is readable only by admins, and the raw tool
observation is a second door (``include_raw=true``) that writes an audit row.
The user-facing replay endpoint ``/v1/runs/{run_id}`` keeps its old shape —
run records add evidence, they don't widen the user surface.
"""

from __future__ import annotations

import pytest

from trove.core.types import Message
from trove.storage.run_record_store import RunRecordStore

RUN_ID = "run-record-1"


def _record_events():
    return [
        {"seq": 0, "kind": "run", "payload": {"workflow": "reflection"}},
        {"seq": 1, "kind": "llm", "payload": {
            "node": "gen_generate", "model": "mock/model",
            "messages": [{"role": "user", "content": "SECRET-PROMPT"}],
            "output": "SELECT 1",
        }},
        {"seq": 2, "kind": "tool", "payload": {
            "name": "probe_query", "tool_call_id": "c1",
            "arguments": {"sql": "SELECT 1"}, "observation": "SECRET-OBS",
            "elapsed_ms": 7.5, "error": None,
        }},
        {"seq": 3, "kind": "tool_model", "payload": {
            "tool_call_id": "c1", "content": "SECRET-OBS",
        }},
    ]


@pytest.fixture
async def record_store(api_app, tmp_path):
    store = RunRecordStore(tmp_path)
    await store.write(
        {
            "run_id": RUN_ID, "session_id": "s1", "question": "q?",
            "workflow": "reflection", "datasource": "test_db",
            "status": "done", "finish_json": {"sql": "SELECT 1", "verdict": "OK"},
            "finished_at": "2026-01-01T00:00:01+00:00",
        },
        _record_events(),
    )
    api_app.state.run_records = store
    try:
        yield store
    finally:
        await store.dispose()


class TestReadEndpoint:
    async def test_admin_reads_stripped_record(self, client, api_app, record_store):
        resp = await client.get(f"/v1/admin/runs/{RUN_ID}/record")
        assert resp.status_code == 200
        body = resp.json()
        assert body["header"]["status"] == "done"
        assert body["header"]["finish"]["sql"] == "SELECT 1"
        kinds = [e["kind"] for e in body["events"]]
        assert kinds == ["run", "llm", "tool", "tool_model"]
        tool = body["events"][2]["payload"]
        assert "observation" not in tool
        assert tool["name"] == "probe_query"
        assert tool["arguments"] == {"sql": "SELECT 1"}
        assert tool["elapsed_ms"] == 7.5
        # LLM messages/output are the model-visible side of the record and
        # stay in the default read (that is what "what did the model see" means).
        llm = body["events"][1]["payload"]
        assert llm["messages"][0]["content"] == "SECRET-PROMPT"
        # Stripped reads are not audited — they're the normal admin view.
        rows = await api_app.state.auth.list_audit(
            action="admin.run_record.read_raw")
        assert rows == []

    async def test_include_raw_returns_observation_and_audits(
            self, client, api_app, record_store):
        resp = await client.get(
            f"/v1/admin/runs/{RUN_ID}/record?include_raw=true")
        assert resp.status_code == 200
        body = resp.json()
        tool = next(e for e in body["events"] if e["kind"] == "tool")
        assert tool["payload"]["observation"] == "SECRET-OBS"

        rows = await api_app.state.auth.list_audit(
            action="admin.run_record.read_raw")
        assert len(rows) == 1
        assert RUN_ID in str(rows[0]["details"])

    async def test_missing_run_is_404(self, client, record_store):
        resp = await client.get("/v1/admin/runs/nope/record")
        assert resp.status_code == 404

    async def test_outcome_is_projected_from_finish_and_status(
            self, client, record_store):
        """读端不解析 finish_json:done 的三分别由 ``outcome`` 一次说清。

        ``running``/无 finish 的 ``done`` 是 ``None``(三态,不猜)——
        把生命周期的 done 冒充成「答复」正是这一层要防的。
        """
        await record_store.write(
            {"run_id": "run-refused", "session_id": "s1", "question": "q?",
             "workflow": "reflection", "datasource": "test_db",
             "status": "done", "finish_json": {"outcome": "refused"},
             "finished_at": "2026-01-01T00:00:02+00:00"}, [])
        body = (await client.get("/v1/admin/runs/run-refused/record")).json()
        assert body["outcome"] == "refused"

        await record_store.write(
            {"run_id": "run-pending", "session_id": "s1", "question": "q?",
             "workflow": "reflection", "datasource": "test_db",
             "status": "pending_hitl"}, [])
        body = (await client.get("/v1/admin/runs/run-pending/record")).json()
        assert body["outcome"] == "pending_approval"

        # fixture 的记录 status=done 但 finish 里没有 outcome(旧记录)。
        body = (await client.get(f"/v1/admin/runs/{RUN_ID}/record")).json()
        assert body["outcome"] is None

    async def test_unassembled_store_is_503(self, client):
        # The conftest app has no run_records component.
        resp = await client.get(f"/v1/admin/runs/{RUN_ID}/record")
        assert resp.status_code == 503


class TestAccess:
    async def test_user_forbidden(self, user_client, record_store):
        resp = await user_client.get(f"/v1/admin/runs/{RUN_ID}/record")
        assert resp.status_code == 403

    async def test_anon_unauthorized(self, anon_client, record_store):
        resp = await anon_client.get(f"/v1/admin/runs/{RUN_ID}/record")
        assert resp.status_code == 401


class TestUserReplayUnchanged:
    async def test_runs_endpoint_has_no_record_fields(
            self, client, api_app, record_store):
        """``/v1/runs/{run_id}`` 不读 run record,两种参数下都不含原文。"""
        manager = api_app.state.session_manager
        session = await manager.start_session()
        session.messages.append(Message(role="user", content="q?"))
        session.messages.append(Message(
            role="assistant", content="answer",
            metadata={"summary": {
                "run_id": RUN_ID, "question": "q?", "sql": "SELECT 1",
                "verdict": "OK",
            }},
        ))
        await manager.save_session(session)

        for params in ({}, {"include_raw": "true"}):
            resp = await client.get(f"/v1/runs/{RUN_ID}", params=params)
            assert resp.status_code == 200
            text = resp.text
            assert "SECRET-OBS" not in text
            assert "SECRET-PROMPT" not in text
            body = resp.json()
            assert "events" not in body
            assert "messages" not in body
