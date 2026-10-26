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


def _manager(tmp_home, sqlite_registry, responses, *, hitl=False,
             result_cache=False):
    from langgraph.checkpoint.memory import InMemorySaver

    from trove.agent.session import SessionManager
    from trove.storage.session_store import SessionStore

    config = AgentConfig(
        home=str(tmp_home), target="mock/model", hitl=hitl,
        result_cache=result_cache,
    )
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
        # 非流式路径与流式同一条结算:finish 带耗时与 token 汇总
        # (此前 ask() 从不调 _run_stats,运行记录缺这两项)。
        assert isinstance(header["finish"]["total_elapsed_ms"], int)
        assert header["finish"]["token_usage"]["total"] == 15  # 3 次 × 5

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


async def test_cache_hit_finish_is_marked_and_zero_cost(
        tmp_home, sqlite_registry, store):
    """命中轮的可观测事实:零 LLM、无 token、耗时实测、``cached=True``。

    账本靠 finish.cached 区分「零成本的一轮」与「跑过但没测到成本」;
    这里同时钉住 ask() 的缓存分支也结算 stats。
    """
    from trove.core.run_ledger import build_run_ledger

    manager = _manager(
        tmp_home, sqlite_registry, ["query", _SQL, "OK"], result_cache=True)
    try:
        session = await manager.start_session(project_cwd="/tmp/p")
        first = await manager.ask(session=session, question=_Q)
        second = await manager.ask(session=session, question=_Q)
        assert second.cached is True
        await drain_run_records()

        run_record = await store.get(first.run_id)
        finish = run_record["header"]["finish"]
        assert finish["cached"] is False
        assert finish["token_usage"]["total"] == 15
        first_ledger = build_run_ledger(run_record)
        assert first_ledger["cache"]["answer_cached"] is False
        assert first_ledger["llm"]["calls"] == 3
        assert first_ledger["llm"]["tokens_source"] == "finish"
        assert first_ledger["llm"]["tokens"]["total"] == 15

        hit_record = await store.get(second.run_id)
        finish = hit_record["header"]["finish"]
        assert finish["cached"] is True
        assert isinstance(finish["total_elapsed_ms"], int)
        # 命中轮没调 LLM → 没有 token 事实(不是「测到 0」)。
        assert "token_usage" not in finish
        ledger = build_run_ledger(hit_record)
        assert ledger["cache"]["answer_cached"] is True
        assert ledger["llm"]["calls"] == 0
        assert ledger["llm"]["tokens_source"] is None
        assert "tokens_unmeasured" in ledger["coverage"]
    finally:
        await manager.dispose()


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
        # 续跑在 finish 之前结算整条 tally(中断前的调用也在其中),而不是
        # 只把统计塞进 SSE summary。
        assert isinstance(record["header"]["finish"]["total_elapsed_ms"], int)
        assert record["header"]["finish"]["token_usage"]["total"] > 0
        seqs_after = [e["seq"] for e in record["events"]]
        assert seqs_after[: len(seqs_before)] == seqs_before
        assert seqs_after == list(range(len(seqs_after)))
        kinds = [e["kind"] for e in record["events"]]
        assert kinds.count("hitl") == 1
    finally:
        await manager.dispose()
