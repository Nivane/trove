"""``RunRecorder`` — synchronous capture, asynchronous single-flight flush.

The answer path must never await (or fail because of) its own evidence
trail; these tests pin that at the recorder level: capture never touches the
store, flush is bounded and warning-only, the switch turns everything off,
and overflow gives up evidence in a defined order instead of silently
dropping the run.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import trove.tracing.run_record as run_record
from trove.tracing.run_record import (
    MAX_LLM_OUTPUT_BYTES,
    MAX_TOOL_OBSERVATION_BYTES,
    RunRecorder,
    configure_run_records,
    get_run_recorder,
    is_run_records_enabled,
    reset_run_records,
)


class FakeStore:
    """Records (header, events) snapshots; optional gate blocks the write."""

    def __init__(self, *, gate: asyncio.Event | None = None,
                 fail: bool = False):
        self.writes: list[tuple[dict, list[dict]]] = []
        self.gate = gate
        self.fail = fail

    async def write(self, header, events):
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("store down")
        self.writes.append((dict(header), [dict(e) for e in events]))
        return len(events)


def _final(**over):
    base = {"sql": "SELECT 1", "verdict": "OK", "row_count": 1, "error": ""}
    base.update(over)
    return SimpleNamespace(**base)


class TestCaptureBasics:
    async def test_run_start_flushes_run_event(self):
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start(
            "r1", session_id="s1", question="q?", workflow="reflection",
            datasource="test_db", lang="zh",
        )
        await rec.drain()

        assert len(store.writes) == 1
        header, events = store.writes[0]
        assert header["run_id"] == "r1"
        assert header["status"] == "running"
        assert header["session_id"] == "s1"
        assert [e["kind"] for e in events] == ["run"]
        assert events[0]["seq"] == 0
        assert events[0]["payload"]["workflow"] == "reflection"

    async def test_capture_is_synchronous(self):
        """capture returns before the (gated) store write completes."""
        gate = asyncio.Event()
        rec = RunRecorder(FakeStore(gate=gate))
        rec.capture_run_start("r1", question="q?")
        # No await since capture — the flush task exists but is blocked.
        assert getattr(rec, "_buffers")["r1"].task is not None
        gate.set()
        await rec.drain()

    async def test_capture_snapshots_mutable_arguments(self):
        """log-as-truth: the record is what the model saw *at that call*.

        The agent loop hands the gateway its live ``messages`` list and
        appends tool results to it afterwards; storing the reference would
        make round-1 LLM events show later turns once the buffer flushes.
        The same applies to tool ``arguments`` dicts.
        """
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1")
        messages = [{"role": "user", "content": "q?"}]
        rec.capture_llm("r1", messages=messages, output="SELECT 1")
        messages.append({"role": "tool", "content": "later turn"})
        args = {"sql": "SELECT 1"}
        rec.capture_tool("r1", name="probe_query", arguments=args)
        args["sql"] = "SELECT 2"  # mutation must not reach the record
        rec.capture_finish("r1", final=_final())
        await rec.drain()

        _, events = store.writes[-1]
        llm = next(e for e in events if e["kind"] == "llm")
        assert llm["payload"]["messages"] == [
            {"role": "user", "content": "q?"}]
        tool = next(e for e in events if e["kind"] == "tool")
        assert tool["payload"]["arguments"] == {"sql": "SELECT 1"}

    async def test_finish_writes_projection_and_status(self):
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1", question="q?")
        rec.capture_llm("r1", node="gen_sql", model="m",
                        messages=[{"role": "user", "content": "q?"}],
                        output="SELECT 1")
        rec.capture_finish("r1", final=_final(), assembly={"blocks": []})
        await rec.drain()

        header, events = store.writes[-1]
        assert header["status"] == "done"
        assert header["finish_json"]
        assert '"SELECT 1"' in header["finish_json"]
        assert header["assembly_json"]
        assert [e["kind"] for e in events] == ["run", "llm"]
        assert header["finished_at"]

    async def test_finish_carries_the_answer_cache_fact(self):
        """缓存命中是运行事实:finish 里必须能读出「这一轮是命中的」。

        运行账本靠它区分「零成本的一轮」与「跑过但没测到成本的一轮」;
        False 也是**测到的事实**(跑过、未命中),不能被当成空值丢掉。
        """
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1")
        rec.capture_finish("r1", final=_final(cached=True))
        await rec.drain()
        assert json.loads(store.writes[-1][0]["finish_json"])["cached"] is True

        rec.capture_run_start("r2")
        rec.capture_finish("r2", final=_final(cached=False))
        await rec.drain()
        assert json.loads(store.writes[-1][0]["finish_json"])["cached"] is False

    async def test_finish_projection_omits_and_caps_rows(self):
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1")
        rec.capture_finish(
            "r1", summary={
                "sql": "SELECT 1",
                "rows_preview": [[i] for i in range(80)],
                "chart": {"type": "bar"},
                "steps": [{"n": 1}],
            },
        )
        await rec.drain()
        header, _ = store.writes[-1]
        import json
        finish = json.loads(header["finish_json"])
        assert len(finish["rows_preview"]) == 50
        assert "chart" in finish["omitted"]
        assert "steps" in finish["omitted"]

    async def test_finish_with_error_marks_failed(self):
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1")
        rec.capture_finish("r1", final=_final(error="boom"))
        await rec.drain()
        header, _ = store.writes[-1]
        assert header["status"] == "failed"
        assert header["error"] == "boom"

    async def test_hitl_then_finish_updates_one_header(self):
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1", question="q?")
        rec.capture_hitl("r1", sql="SELECT 1", question="q?",
                         summary={"sql": "SELECT 1"})
        await rec.drain()
        assert store.writes[-1][0]["status"] == "pending_hitl"
        assert store.writes[-1][0]["finished_at"] is None

        rec.capture_finish("r1", final=_final())
        await rec.drain()
        header, events = store.writes[-1]
        assert header["status"] == "done"
        assert [e["kind"] for e in events] == ["run", "hitl"]

    async def test_error_and_cancel_are_terminal(self):
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1")
        rec.capture_error("r1", error="boom", error_info={"title": "boom"})
        await rec.drain()
        assert store.writes[-1][0]["status"] == "error"

        rec.capture_cancel("r1")
        await rec.drain()
        assert store.writes[-1][0]["status"] == "cancelled"


class TestLifecycleFunnel:
    """``_transition`` 是 ``status`` / ``finished_at`` 的唯一写点（P3）。

    状态说「终态」而 ``finished_at`` 还空着（或反过来），是一条自相矛盾的
    记录；两个字段必须由同一次转移一起写。
    """

    def _buffer(self):
        rec = RunRecorder(FakeStore())
        rec.capture_run_start("r1", question="q?")
        return rec._buffers["r1"]

    def test_hitl_is_a_pause_not_a_finish(self):
        buf = self._buffer()
        run_record._transition(buf, "pending_hitl", finished=False)
        assert buf.header["status"] == "pending_hitl"
        assert buf.header["finished_at"] is None

    def test_resuming_from_hitl_clears_the_pause(self):
        # HITL 恢复后 buffer 被淘汰重建时,初始 running 不能带着旧 finished_at。
        buf = self._buffer()
        run_record._transition(buf, "pending_hitl", finished=False)
        run_record._transition(buf, "running", finished=False)
        assert buf.header["finished_at"] is None

    @pytest.mark.parametrize(
        "status", ["done", "failed", "error", "cancelled"])
    def test_every_terminal_state_stamps_finished_at(self, status):
        buf = self._buffer()
        run_record._transition(buf, status, finished=True)
        assert buf.header["finished_at"]

    def test_unknown_status_raises(self):
        buf = self._buffer()
        with pytest.raises(ValueError, match="unknown run record status"):
            run_record._transition(buf, "weird", finished=True)


class TestFinishOutcome:
    """``finish_json.outcome``：done 的记录里，答复/拒绝/澄清的区别只在这里。

    header.status 对三者都是 ``done``（生命周期终点），拒绝审批也是 done ——
    结局字段缺席或错分，回看记录的人就分不清这一轮到底交没交出答案。
    """

    async def test_final_path_carries_the_classified_outcome(self):
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1")
        rec.capture_finish("r1", final=_final())
        await rec.drain()
        finish = json.loads(store.writes[-1][0]["finish_json"])
        assert finish["outcome"] == "answered"

    @pytest.mark.parametrize(
        ("over", "expected"),
        [
            ({"refusal": {"reason": "no_model"},
              "clarification_question": "?"}, "refused"),
            ({"clarification_question": "?"}, "clarified"),
            ({"error": "boom"}, "failed"),
            ({"hitl_status": "rejected"}, "cancelled"),
        ],
    )
    async def test_terminal_states_stay_distinguishable(self, over, expected):
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1")
        rec.capture_finish("r1", final=_final(**over))
        await rec.drain()
        header = store.writes[-1][0]
        finish = json.loads(header["finish_json"])
        assert finish["outcome"] == expected
        if expected == "cancelled":
            # HITL 否决没有执行错误：header 仍是 done，结局是唯一的分辨点。
            assert header["status"] == "done"

    async def test_summary_path_keeps_the_session_outcome(self):
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1")
        rec.capture_finish("r1", summary={"outcome": "refused", "sql": ""})
        await rec.drain()
        finish = json.loads(store.writes[-1][0]["finish_json"])
        assert finish["outcome"] == "refused"

    async def test_outcome_survives_the_size_cap(self):
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1")
        rec.capture_finish(
            "r1",
            summary={
                "outcome": "answered",
                # 先被截到 50 行，仍超 256KB —— 触发整节省略(rows_preview)。
                "rows_preview": [["x" * 8192] for _ in range(100)],
            },
        )
        await rec.drain()
        finish = json.loads(store.writes[-1][0]["finish_json"])
        # rows_preview 被裁剪，结局是头等字段，永不让路。
        assert "rows_preview" not in finish
        assert finish["outcome"] == "answered"
        assert "outcome" not in finish.get("truncated_keys", [])


class TestSwitch:
    async def test_disabled_recorder_never_buffers(self):
        store = FakeStore()
        rec = RunRecorder(store, enabled=False)
        rec.capture_run_start("r1", question="q?")
        rec.capture_finish("r1", final=_final())
        await rec.drain()
        assert store.writes == []
        assert rec._buffers == {}

    async def test_enabled_callable_is_read_per_capture(self):
        store = FakeStore()
        flag = {"on": False}
        rec = RunRecorder(store, enabled=lambda: flag["on"])
        rec.capture_run_start("r1", question="q?")
        assert rec._buffers == {}
        flag["on"] = True
        rec.capture_run_start("r1", question="q?")
        await rec.drain()
        assert len(store.writes) == 1

    async def test_enabled_callable_failure_is_off(self):
        rec = RunRecorder(FakeStore(), enabled=lambda: 1 / 0)
        assert rec.enabled() is False

    async def test_store_none_is_off(self):
        rec = RunRecorder(None)
        assert rec.enabled() is False


class TestFailureIsolation:
    async def test_store_failure_does_not_raise_or_retry(self, caplog):
        store = FakeStore(fail=True)
        rec = RunRecorder(store)
        rec.capture_run_start("r1", question="q?")
        await rec.drain()  # never raises
        assert store.writes == []
        assert any("run-record flush failed" in r.message for r in caplog.records)

    async def test_single_flight_marks_dirty(self):
        gate = asyncio.Event()
        store = FakeStore(gate=gate)
        rec = RunRecorder(store)
        rec.capture_run_start("r1", question="q?")  # first write in flight
        rec.capture_llm("r1", output="SELECT 1")
        rec.schedule_flush("r1")  # in-flight → dirty
        await asyncio.sleep(0)
        gate.set()
        await rec.drain()

        assert len(store.writes) == 2
        assert [e["kind"] for e in store.writes[-1][1]] == ["run", "llm"]


class TestCaps:
    async def test_llm_output_and_messages_are_capped(self):
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1")
        rec.capture_llm(
            "r1",
            messages=[{"role": "user", "content": "x" * 200_000},
                      {"role": "assistant", "content": "y" * 200_000}],
            output="z" * (MAX_LLM_OUTPUT_BYTES + 100),
        )
        rec.capture_finish("r1", final=_final())
        await rec.drain()
        header, events = store.writes[-1]
        llm = next(e for e in events if e["kind"] == "llm")
        payload = llm["payload"]
        assert len(payload["output"].encode()) <= MAX_LLM_OUTPUT_BYTES
        assert payload["output_truncated_bytes"] > 0
        assert "messages_truncated_bytes" in payload
        assert llm["truncated"] is True
        assert header["truncated"] == 1
        assert "llm_messages_truncated" in header["truncation_json"]

    async def test_tool_observation_is_capped(self):
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1")
        rec.capture_tool(
            "r1", name="probe_query",
            observation="r" * (MAX_TOOL_OBSERVATION_BYTES + 10),
        )
        rec.capture_finish("r1", final=_final())
        await rec.drain()
        _, events = store.writes[-1]
        tool = next(e for e in events if e["kind"] == "tool")
        assert len(tool["payload"]["observation"].encode()) <= MAX_TOOL_OBSERVATION_BYTES
        assert tool["payload"]["observation_truncated_bytes"] > 0

    async def test_tool_model_is_capped_tighter(self):
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1")
        rec.capture_tool_model("r1", tool_call_id="c1", content="m" * 20_000)
        rec.capture_finish("r1", final=_final())
        await rec.drain()
        _, events = store.writes[-1]
        tm = next(e for e in events if e["kind"] == "tool_model")
        assert len(tm["payload"]["content"].encode()) <= run_record.MAX_TOOL_MODEL_BYTES
        assert tm["payload"]["truncated_bytes"] > 0

    async def test_event_count_overflow_drops_oldest_tool_model(self, monkeypatch):
        monkeypatch.setattr(run_record, "MAX_RUN_EVENTS", 3)
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1")  # run (seq 0)
        rec.capture_tool_model("r1", tool_call_id="c1", content="seen")  # seq 1
        rec.capture_tool("r1", name="t1", observation="o1")  # seq 2
        rec.capture_tool("r1", name="t2", observation="o2")  # overflow → drop tm
        rec.capture_finish("r1", final=_final())
        await rec.drain()
        header, events = store.writes[-1]
        kinds = [e["kind"] for e in events]
        assert kinds == ["run", "tool", "tool"]
        assert "tool_model_dropped" in header["truncation_json"]

    async def test_byte_overflow_drops_new_observation_not_old_evidence(
            self, monkeypatch):
        monkeypatch.setattr(run_record, "MAX_RUN_BYTES", 1500)
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1", question="q?")
        rec.capture_tool("r1", name="t1", observation="small")
        rec.capture_tool("r1", name="t2", observation="z" * 5000)
        rec.capture_finish("r1", final=_final())
        await rec.drain()
        header, events = store.writes[-1]
        tool2 = [e for e in events if e["kind"] == "tool"][-1]
        assert "observation" not in tool2["payload"]
        assert tool2["payload"].get("observation_dropped") is True
        assert "tool_observations_dropped" in header["truncation_json"]
        # The older evidence survives: new raw observations yield first.
        first_tool = [e for e in events if e["kind"] == "tool"][0]
        assert first_tool["payload"]["observation"] == "small"

    async def test_process_buffer_cap_flushes_the_oldest(self, monkeypatch):
        monkeypatch.setattr(run_record, "MAX_BUFFERS", 2)
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1", question="q1")
        await asyncio.sleep(0)
        await rec.drain()
        rec.capture_run_start("r2", question="q2")
        await rec.drain()
        rec.capture_run_start("r3", question="q3")  # evicts r1 (already flushed)
        await rec.drain()
        assert "r1" not in rec._buffers
        assert set(rec._buffers) == {"r2", "r3"}


class TestRecoveryFidelity:
    async def test_identity_survives_buffer_eviction(self, tmp_path):
        """start 带身份 → 淘汰 → 空身份 finish → 读回身份仍在（P3 修复）。

        HITL 恢复 / 进程重启后 buffer 是全新的（身份为空）；终态落盘只能用
        既有行补齐，不能把库里已存的 session/user/question 抹掉。
        """
        from trove.storage.run_record_store import RunRecordStore

        store = RunRecordStore(tmp_path)
        try:
            rec = RunRecorder(store)
            rec.capture_run_start(
                "r1", session_id="s1", user_id="u1", question="q?",
                workflow="reflection", datasource="test_db")
            await rec.drain()
            # 模拟进程内驱逐:首轮 flush 已落盘,buffer 没了。
            rec._buffers.pop("r1")
            rec._total_bytes = 0

            rec.capture_finish("r1", final=_final())
            await rec.drain()

            header = (await store.get("r1"))["header"]
            assert header["session_id"] == "s1"
            assert header["user_id"] == "u1"
            assert header["question"] == "q?"
            assert header["workflow"] == "reflection"
            assert header["datasource"] == "test_db"
            assert header["status"] == "done"
            assert header["finish"]["sql"] == "SELECT 1"
            assert header["finish"]["outcome"] == "answered"
        finally:
            await store.dispose()

    async def test_resumed_buffer_events_are_appended_not_dropped(
            self, tmp_path):
        """HITL 恢复后新 buffer 的 seq 从 0 重来,但库里已有 seq 0/1 —— 事件必须追加。

        只做 ``ON CONFLICT DO NOTHING`` 时,恢复后那轮 flush 的 llm/tool 事件
        会被旧 seq 挡掉,终态记录只剩暂停前那几条（P3 修复）。
        """
        from trove.storage.run_record_store import RunRecordStore

        store = RunRecordStore(tmp_path)
        try:
            rec = RunRecorder(store)
            rec.capture_run_start("r1", question="q?")
            rec.capture_hitl("r1", sql="SELECT 1", question="q?")
            await rec.drain()
            assert [e["seq"] for e in (await store.get("r1"))["events"]] == [0, 1]

            rec._buffers.pop("r1")  # HITL 恢复 / 淘汰：buffer 没了
            rec._total_bytes = 0
            rec.capture_llm("r1", output="SELECT 1")
            rec.capture_tool("r1", name="check_result", observation="ok")
            rec.capture_finish("r1", final=_final())
            await rec.drain()

            record = await store.get("r1")
            assert [e["kind"] for e in record["events"]] == [
                "run", "hitl", "llm", "tool"]
            assert [e["seq"] for e in record["events"]] == [0, 1, 2, 3]
            assert record["events"][3]["payload"]["name"] == "check_result"
            assert record["header"]["status"] == "done"
        finally:
            await store.dispose()


class TestModuleSingleton:
    async def test_configure_enable_reset(self):
        store = FakeStore()
        try:
            configure_run_records(store, enabled=True)
            assert is_run_records_enabled() is True
            from trove.tracing.run_record import capture_run_start
            capture_run_start("r1", question="q?")
            rec = get_run_recorder()
            assert rec is not None
            await rec.drain()
            assert len(store.writes) == 1
        finally:
            reset_run_records()
        assert is_run_records_enabled() is False
        assert get_run_recorder() is None

    async def test_capture_without_recorder_is_a_noop(self):
        reset_run_records()
        from trove.tracing.run_record import capture_llm
        capture_llm("r1", output="x")  # no exception, nothing to drain


class TestDrain:
    async def test_drain_is_bounded_when_the_store_hangs(self):
        gate = asyncio.Event()  # never set
        rec = RunRecorder(FakeStore(gate=gate))
        rec.capture_run_start("r1", question="q?")
        await rec.drain(timeout=0.01)  # returns without raising
        rec.reset()  # cancels the hung task

    async def test_reset_cancels_tasks_and_clears(self):
        gate = asyncio.Event()
        rec = RunRecorder(FakeStore(gate=gate))
        rec.capture_run_start("r1", question="q?")
        rec.reset()
        assert rec._buffers == {}
        assert rec._total_bytes == 0


@pytest.mark.parametrize("kind", ["run", "llm", "tool", "tool_model", "hitl"])
def test_capture_methods_need_a_run_id(kind):
    rec = RunRecorder(FakeStore())
    # All capture methods tolerate an empty run id (the tracer may call them
    # before a run is minted); nothing is buffered under "".
    if kind == "run":
        rec.capture_run_start("", question="q")
    elif kind == "llm":
        rec.capture_llm("", output="x")
    elif kind == "tool":
        rec.capture_tool("", name="t")
    elif kind == "tool_model":
        rec.capture_tool_model("", tool_call_id="c", content="x")
    else:
        rec.capture_hitl("", sql="", question="")
    assert rec._buffers == {}


class TestRecordVersionStamp:
    async def test_header_carries_the_record_version_stamp(self):
        """录制端写出的 header 自带 record_version=1(注册表 run-record-version)。"""
        store = FakeStore()
        rec = RunRecorder(store)
        rec.capture_run_start("r1", session_id="s1", question="q?")
        await rec.drain()

        header, _events = store.writes[0]
        assert header["record_version"] == 1
