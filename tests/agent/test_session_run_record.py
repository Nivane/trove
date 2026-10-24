"""Session-level request records: one run, one durable header.

The session is where a run gets its identity (``run_id``/``run_id_var``) and
where its lifecycle is known (start → HITL → finish / error / cancel). These
tests drive the real graph and pin the properties the evidence trail needs:
a normal ask leaves a done record with the finish projection and LLM calls
accounted to the same run; capture never blocks or breaks the answer; the
switch leaves no database behind; HITL is one header updated in place with
contiguous sequence numbers.
"""

from __future__ import annotations

import asyncio

import pytest

from trove.core.config import AgentConfig
from trove.llm.gateway import _record_local_call
from trove.storage.run_record_store import DB_FILE_NAME, RunRecordStore
from trove.tracing.run_record import (
    configure_run_records,
    drain_run_records,
    reset_run_records,
)
from trove.workflow.graphs import GraphServices, build_graphs


class RecordingLLM:
    """Scripted gateway that goes through the gateway's own capture path.

    Calling ``_record_local_call`` without metadata.run_id is deliberate: it
    proves the session stamped ``run_id_var`` before the graph ran, which is
    the only reason most pipeline calls land in the record at all.
    """

    def __init__(self, responses):
        self._it = iter(responses)

    def _record(self, model, messages, output):
        _record_local_call(
            model, messages, output, None, 1,
            usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        )

    async def chat(self, model, messages, **kwargs):
        out = next(self._it)
        self._record(model, messages, out)
        return out

    async def chat_full(self, model, messages, tools=None, **kwargs):
        out = next(self._it)
        self._record(model, messages, out)
        return {"content": out, "tool_calls": []}


def _manager(tmp_home, sqlite_registry, responses, *, hitl=False):
    from langgraph.checkpoint.memory import InMemorySaver

    from trove.agent.session import SessionManager
    from trove.storage.session_store import SessionStore

    config = AgentConfig(home=str(tmp_home), target="mock/model", hitl=hitl)
    llm = RecordingLLM(responses)
    graphs = build_graphs(
        GraphServices(
            llm=llm, connectors=sqlite_registry,
            semantic_layer=getattr(sqlite_registry, "_test_semantic_provider", None),
            config=config,
        ),
        checkpointer=InMemorySaver(),
        multi_candidate=False, query_sketch=False, agentic=False,
    )
    return SessionManager(
        config=config,
        session_store=SessionStore(home_dir=str(tmp_home)),
        graphs=graphs,
        llm_gateway=llm,
    )


_SQL = "```sql\nSELECT name FROM students;\n```"
_Q = "What students are in Alameda county?"


@pytest.fixture
async def store(tmp_home):
    s = RunRecordStore(tmp_home)
    configure_run_records(s, enabled=True)
    try:
        yield s
    finally:
        await drain_run_records()
        await s.dispose()
        reset_run_records()


async def test_ask_writes_a_done_record(tmp_home, sqlite_registry, store):
    manager = _manager(tmp_home, sqlite_registry, ["query", _SQL, "OK"])
    try:
        session = await manager.start_session(project_cwd="/tmp/p")
        final = await manager.ask(session=session, question=_Q)
        assert final.verdict == "OK"
        await drain_run_records()

        record = await store.get(final.run_id)
        assert record is not None
        header = record["header"]
        assert header["status"] == "done"
        assert header["session_id"] == session.session_id
        assert header["question"] == _Q
        assert header["workflow"] == "reflection"
        assert header["finished_at"]
        assert header["finish"]["sql"] == "SELECT name FROM students;"
        assert header["finish"]["verdict"] == "OK"
        assert header["assembly"]

        kinds = [e["kind"] for e in record["events"]]
        assert kinds[0] == "run"
        assert "llm" in kinds
        assert [e["seq"] for e in record["events"]] == list(
            range(len(record["events"])))
        # The LLM call carried no run id of its own — the session's
        # run_id_var carried it into the same record.
        llm = next(e for e in record["events"] if e["kind"] == "llm")
        assert llm["payload"]["model"] == "mock/model"
    finally:
        await manager.dispose()


async def test_capture_does_not_block_the_answer(
        tmp_home, sqlite_registry):
    gate = asyncio.Event()

    class GatedStore:
        def __init__(self):
            self.started = asyncio.Event()
            self.writes = []

        async def write(self, header, events):
            self.started.set()
            await gate.wait()
            self.writes.append((header, events))
            return len(events)

    gated = GatedStore()
    configure_run_records(gated, enabled=True)
    manager = _manager(tmp_home, sqlite_registry, ["query", _SQL, "OK"])
    try:
        session = await manager.start_session(project_cwd="/tmp/p")
        final = await asyncio.wait_for(
            manager.ask(session=session, question=_Q), timeout=5.0)
        assert final.final_response
        # The flush is in flight and blocked; the answer still went out.
        await asyncio.sleep(0)
        assert gated.started.is_set()
        assert gated.writes == []

        gate.set()
        await drain_run_records()
        assert gated.writes
    finally:
        gate.set()
        await manager.dispose()
        reset_run_records()


async def test_store_failure_does_not_change_the_answer(
        tmp_home, sqlite_registry):

    class BrokenStore:
        async def write(self, header, events):
            raise RuntimeError("disk on fire")

    configure_run_records(BrokenStore(), enabled=True)
    manager = _manager(tmp_home, sqlite_registry, ["query", _SQL, "OK"])
    try:
        session = await manager.start_session(project_cwd="/tmp/p")
        final = await manager.ask(session=session, question=_Q)
        assert final.verdict == "OK"
        assert final.sql == "SELECT name FROM students;"
        assert final.row_count == 5
        await drain_run_records()  # never raises
    finally:
        await manager.dispose()
        reset_run_records()


async def test_switch_off_leaves_no_database(tmp_home, sqlite_registry):
    s = RunRecordStore(tmp_home)
    configure_run_records(s, enabled=False)
    manager = _manager(tmp_home, sqlite_registry, ["query", _SQL, "OK"])
    try:
        session = await manager.start_session(project_cwd="/tmp/p")
        final = await manager.ask(session=session, question=_Q)
        assert final.verdict == "OK"
        await drain_run_records()
        assert not (tmp_home / DB_FILE_NAME).exists()
    finally:
        await manager.dispose()
        await s.dispose()
        reset_run_records()


async def test_hitl_updates_one_header_across_resume(
        tmp_home, sqlite_registry, store):
    manager = _manager(
        tmp_home, sqlite_registry, ["query", _SQL, "OK"], hitl=True)
    try:
        session = await manager.start_session(project_cwd="/tmp/p")
        pending = await manager.ask(session=session, question=_Q)
        assert pending.hitl_status == "pending"
        run_id = pending.run_id
        await drain_run_records()

        record = await store.get(run_id)
        assert record["header"]["status"] == "pending_hitl"
        assert record["header"]["finished_at"] is None
        seqs_before = [e["seq"] for e in record["events"]]

        loaded = await manager.load_session(session.session_id, "/tmp/p")
        events = [ev async for ev in manager.resume_stream(loaded, "yes")]
        assert events[-1]["type"] == "done"
        await drain_run_records()

        record = await store.get(run_id)
        assert record["header"]["status"] == "done"
        assert record["header"]["finish"]["sql"] == "SELECT name FROM students;"
        seqs_after = [e["seq"] for e in record["events"]]
        assert seqs_after[: len(seqs_before)] == seqs_before
        assert seqs_after == list(range(len(seqs_after)))
        kinds = [e["kind"] for e in record["events"]]
        assert kinds.count("hitl") == 1
    finally:
        await manager.dispose()
