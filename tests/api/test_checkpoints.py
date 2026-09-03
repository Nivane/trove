"""/v1/admin/sessions/{id}/checkpoints — checkpoint timeline / detail / resume.

The graph is compiled with a real MemorySaver so checkpoints are actually
written; the api_app fixture in conftest builds graphs without a saver, so
this file wires its own app with a checkpointer-backed graph.
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver


class _CyclingGateway:
    """Scripted responses that loop forever (initial run + replays both work)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0

    def _next(self):
        resp = self._responses[self._i % len(self._responses)]
        self._i += 1
        return resp

    async def chat(self, model, messages, **kwargs):
        return self._next()

    async def chat_full(self, model, messages, tools=None, **kwargs):
        return {"content": self._next(), "tool_calls": []}


@pytest.fixture
async def ckpt_api_app(api_app, sqlite_registry, agent_config, tmp_path):
    """api_app whose session_manager runs a graph compiled on a MemorySaver.

    The default session_manager fixture builds graphs without a checkpointer;
    here we rebuild the graphs with a MemorySaver and repoint the manager's
    ``_graphs`` so an actual query writes checkpoints (the endpoints read the
    saver, which they reach via app.state.graphs.checkpointer).
    """
    from langgraph.checkpoint.memory import MemorySaver

    from trove.services.datasource.catalog import CatalogService
    from trove.workflow.graphs import GraphServices, build_graphs

    saver = MemorySaver()
    # 循环脚本网关:初始 run 与 checkpoint replay 各自重跑整条管线(route_intent
    # → gen_sql → reflect → insights → conclusion 等),必须永不耗尽。
    manager = api_app.state.session_manager
    llm = _CyclingGateway([
        "query",
        "```sql\nSELECT name FROM students;\n```",
        "OK",
        "insight",
        "conclusion",
    ])
    services = GraphServices(
        llm=llm,
        catalog=CatalogService(sqlite_registry),
        connectors=sqlite_registry,
        semantic_layer=getattr(sqlite_registry, "_test_semantic_provider", None),
        config=agent_config,
    )
    graphs = build_graphs(
        services, checkpointer=saver,
        multi_candidate=False, query_sketch=False, agentic=False,
    )
    app = api_app
    app.state.graphs = graphs
    app.state.checkpointer = saver
    manager._graphs = graphs
    return app


async def _run_one_question(client, ckpt_api_app):
    """Create a session and run one question; return the session_id."""
    manager = ckpt_api_app.state.session_manager
    session = await manager.start_session(project_cwd=".", user_id="admin")
    await manager.ask(session, "Which county has most students?")
    return session.session_id


class TestCheckpointTimeline:
    async def test_list_returns_checkpoints_newest_first(self, client, ckpt_api_app):
        session_id = await _run_one_question(client, ckpt_api_app)
        body = (await client.get(f"/v1/admin/sessions/{session_id}/checkpoints")).json()
        cps = body["checkpoints"]
        assert len(cps) >= 2
        # newest first by checkpoint_id (LangGraph's time-based UUID)
        ids = [c["checkpoint_id"] for c in cps]
        assert ids == sorted(ids, reverse=True)
        for c in cps:
            assert c["checkpoint_id"]
            assert c["thread_id"] == session_id
            # real execution steps (step>=1) carry a producing node; the input
            # (step -1) and the first loop bootstrap (step 0) snapshots do not
            if (c["source"] == "loop" and c["step"] and c["step"] >= 1):
                assert c["node"]
            assert "state" in c

    async def test_timeline_includes_terminal_snapshot(self, client, ckpt_api_app):
        """The newest checkpoint reflects the finished run (question+sql)."""
        session_id = await _run_one_question(client, ckpt_api_app)
        body = (await client.get(f"/v1/admin/sessions/{session_id}/checkpoints")).json()
        newest = body["checkpoints"][0]
        assert newest["state"]["question"] == "Which county has most students?"
        assert newest["state"]["sql"]

    async def test_missing_session_404(self, client, ckpt_api_app):
        resp = await client.get("/v1/admin/sessions/nope/checkpoints")
        assert resp.status_code == 404

    async def test_non_admin_403(self, user_client, ckpt_api_app):
        session_id = await _run_one_question(user_client, ckpt_api_app)
        resp = await user_client.get(f"/v1/admin/sessions/{session_id}/checkpoints")
        assert resp.status_code == 403


class TestCheckpointDetail:
    async def test_detail_returns_full_state(self, client, ckpt_api_app):
        session_id = await _run_one_question(client, ckpt_api_app)
        cps = (await client.get(f"/v1/admin/sessions/{session_id}/checkpoints")).json()["checkpoints"]
        cid = cps[0]["checkpoint_id"]
        detail = (await client.get(
            f"/v1/admin/sessions/{session_id}/checkpoints/{cid}"
        )).json()
        assert detail["checkpoint_id"] == cid
        assert "state_full" in detail
        assert detail["state_full"]["question"] == "Which county has most students?"

    async def test_detail_missing_checkpoint_404(self, client, ckpt_api_app):
        session_id = await _run_one_question(client, ckpt_api_app)
        resp = await client.get(
            f"/v1/admin/sessions/{session_id}/checkpoints/does-not-exist"
        )
        assert resp.status_code == 404


class TestCheckpointResume:
    async def test_resume_replays_and_returns_summary(self, client, ckpt_api_app):
        session_id = await _run_one_question(client, ckpt_api_app)
        cps = (await client.get(f"/v1/admin/sessions/{session_id}/checkpoints")).json()["checkpoints"]
        # resume from the earliest snapshot (step -1 input) = full replay
        first = cps[-1]
        resp = await client.post(
            f"/v1/admin/sessions/{session_id}/checkpoints/{first['checkpoint_id']}/resume",
            json={"workflow": "reflection"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["workflow"] == "reflection"
        assert body["summary"]["question"] == "Which county has most students?"
        assert body["summary"]["sql"]
        assert body["summary"]["final_response"]
        # replay writes new checkpoints (timeline grows)
        after = (await client.get(f"/v1/admin/sessions/{session_id}/checkpoints")).json()["checkpoints"]
        assert len(after) >= len(cps)

    async def test_resume_unknown_workflow_400(self, client, ckpt_api_app):
        session_id = await _run_one_question(client, ckpt_api_app)
        cps = (await client.get(f"/v1/admin/sessions/{session_id}/checkpoints")).json()["checkpoints"]
        resp = await client.post(
            f"/v1/admin/sessions/{session_id}/checkpoints/{cps[-1]['checkpoint_id']}/resume",
            json={"workflow": "nope"},
        )
        assert resp.status_code == 400

    async def test_resume_missing_session_404(self, client, ckpt_api_app):
        resp = await client.post(
            "/v1/admin/sessions/nope/checkpoints/xyz/resume", json={},
        )
        assert resp.status_code == 404

    async def test_resume_records_audit(self, client, ckpt_api_app, auth_service):
        session_id = await _run_one_question(client, ckpt_api_app)
        cps = (await client.get(f"/v1/admin/sessions/{session_id}/checkpoints")).json()["checkpoints"]
        await client.post(
            f"/v1/admin/sessions/{session_id}/checkpoints/{cps[-1]['checkpoint_id']}/resume",
            json={},
        )
        entries = await auth_service.list_audit(action="checkpoint.resume")
        assert any(e.get("details", {}).get("session_id") == session_id for e in entries)
