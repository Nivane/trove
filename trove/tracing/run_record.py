"""Run records — synchronous capture, asynchronous persistence.

The tracer emits a complete payload at the moment a model call / tool call
happens, but every existing sink is lossy by design: ``runs/*.log`` keeps the
last 50 files, ``traces.jsonl`` the last 2000 lines, the session store a
200-char preview, and ``offline_eval`` configures no trace store at all. The
run an incident review needs is exactly the one those rings drop first.

This module is the ingest side of ``RunRecordStore``:

- **capture is synchronous** — no ``await``, no task creation, no exception.
  A query must never wait on (or fail because of) its own evidence trail.
- **flush is asynchronous and single-flight per run** — the first flush writes
  a snapshot; while it is in flight further captures only set ``dirty`` and
  the next flush picks them up. Events carry their own ``seq``, so a retried
  flush rewrites the same rows instead of duplicating them.
- **failures are warnings** — a store that is down loses that snapshot; it
  never changes an answer.

Truncation is capped here, not in the store: one oversized tool observation
must not be able to push a run's other evidence out of its budget. Every cap
is recorded in the header's ``truncation`` map — silently short evidence is
worse than visibly short evidence.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from trove.core.run_outcome import classify_run

logger = logging.getLogger(__name__)

# ── caps (all byte budgets are UTF-8) ───────────────────────

MAX_EVENT_BYTES = 256 * 1024
MAX_LLM_MESSAGES_BYTES = 192 * 1024
MAX_LLM_OUTPUT_BYTES = 64 * 1024
MAX_LLM_REASONING_BYTES = 64 * 1024
MAX_TOOL_OBSERVATION_BYTES = 64 * 1024
MAX_TOOL_MODEL_BYTES = 8 * 1024
MAX_FINISH_BYTES = 256 * 1024
MAX_ASSEMBLY_BYTES = 256 * 1024
MAX_RUN_EVENTS = 500
MAX_RUN_BYTES = 8 * 1024 * 1024
MAX_BUFFERS = 200
MAX_TOTAL_BYTES = 64 * 1024 * 1024

#: Drop order when one run exceeds its budget (plan B2):
#: oldest ``tool_model`` → oldest ``llm`` message body → stop capturing new
#: tool raw observations. ``run`` events and the final header are never
#: dropped — the record must at least say *that* a run happened.
_KIND_DROPPABLE = ("tool_model",)

#: Finish projection: what an answer-side reviewer needs, without the full
#: result set / chart payload / step history (all recoverable from the
#: session store; duplicating them here would double the disk for no
#: review value).
_FINISH_KEEP = (
    "run_id", "session_id", "question", "rewritten_question", "datasource",
    "topic", "sql", "model", "row_count", "verdict", "reason",
    "answer_source", "sql_confidence", "confidence", "confidence_evidence",
    "error", "error_info", "refusal", "hitl_status", "final_response",
    # 运行结局:tracing 里唯一能读出「拒绝/澄清/答复」的区别(header.status
    # 对三者都是 done)。不可裁剪 —— 见 _FINISH_DROP_ORDER。
    "outcome",
    "kb_rev", "token_usage", "columns", "matched_tables", "masking_applied",
    "execution_evidence", "rows_preview", "retry_count", "consensus",
    "total_elapsed_ms", "elapsed_ms",
)

#: Order in which optional finish sections are given up under the size cap
#: (largest / least essential first). ``omitted`` records the full list of
#: keys the projection never carried.
_FINISH_DROP_ORDER = (
    "rows_preview", "confidence_evidence", "execution_evidence", "refusal",
    "error_info", "columns",
)

STATUS_RUNNING = "running"
STATUS_PENDING_HITL = "pending_hitl"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

#: 生命周期闭集的镜像常量（权威定义在 ``trove.core.run_outcome`` 的
#: ``RECORD_STATUSES``；tracing 是收口端，不反向定义词汇）。机检钉「集合
#: 相等」，任何一边增删都会红。顺序 = 生命周期推进顺序，不是优先级。
RUN_RECORD_STATUSES: tuple[str, ...] = (
    STATUS_RUNNING,
    STATUS_PENDING_HITL,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_ERROR,
    STATUS_CANCELLED,
)


def _transition(buf: _Buffer, status: str, *, finished: bool) -> None:
    """**唯一**的生命周期转移：status 与 finished_at 一起写。

    四个 capture 方法都改走这里 —— 状态的「是否终态」与 ``finished_at``
    有值/为空是同一件事，各写各的迟早不一致（HITL 恢复时把 ``finished_at``
    留在旧值上，就是一条自相矛盾的记录）。``_Buffer.__init__`` 的初始
    ``running`` 是唯一例外（还没有一次转移）。机检冻结了这个形状：任何
    capture 直写 ``header["status"]`` / ``header["finished_at"]`` 都会红。

    未知状态抛错：闭集不许静默兜底（外层 ``capture_*`` 的 ``_safe`` 会把
    它降级成一条警告，不会让一次运行失败）。
    """
    if status not in RUN_RECORD_STATUSES:
        raise ValueError(f"unknown run record status: {status!r}")
    buf.header["status"] = status
    buf.header["finished_at"] = _now() if finished else None


def _now() -> str:
    from trove.storage.run_record_store import utc_now

    return utc_now()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _bytes(value: Any) -> int:
    return len(_dump(value).encode("utf-8"))


def _cap_text(text: Any, limit: int) -> tuple[Any, int]:
    """Cap one string to ``limit`` UTF-8 bytes; returns ``(text, omitted)``."""
    if not isinstance(text, str):
        return text, 0
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, 0
    kept = raw[:limit].decode("utf-8", "ignore")
    return kept, len(raw) - len(kept.encode("utf-8"))


def _snapshot(value: Any) -> Any:
    """Deep-copy through JSON **at capture time**.

    Callers keep using the objects they hand us — the agent loop's
    ``messages`` list grows after every round, and its early LLM events would
    otherwise show later turns when the buffer finally flushes. The record
    must be what the model saw *then*. Same normalizer as the flush path
    (``default=str``), so nothing captured here can fail to serialize later.
    """
    if value is None:
        return None
    try:
        return json.loads(_dump(value))
    except Exception:
        return value


def _drop_largest(payload: dict[str, Any], keys: tuple[str, ...]) -> bool:
    for key in keys:
        if key in payload:
            payload.pop(key, None)
            return True
    return False


class _Buffer:
    __slots__ = ("run_id", "header", "events", "seq", "dirty", "task", "bytes",
                 "truncated", "truncation")

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.header: dict[str, Any] = {
            "run_id": run_id,
            "session_id": "",
            "user_id": "",
            "question": "",
            "workflow": "",
            "datasource": "",
            "status": STATUS_RUNNING,
            "model": "",
            "kb_rev": "",
            "started_at": _now(),
            "finished_at": None,
            "finish_json": "{}",
            "assembly_json": "{}",
            "error": "",
            "record_version": 1,
            "truncated": 0,
            "truncation_json": "{}",
        }
        self.events: list[dict[str, Any]] = []
        self.seq = -1
        self.dirty = False
        self.task: asyncio.Task | None = None
        self.bytes = 0
        self.truncated = False
        self.truncation: dict[str, int] = {}


class RunRecorder:
    """In-memory buffers + best-effort flush for one process."""

    def __init__(self, store, enabled: bool | Callable[[], bool] = True):
        self._store = store
        self._enabled = enabled
        self._buffers: dict[str, _Buffer] = {}
        self._total_bytes = 0

    # ── configuration ────────────────────────────────────

    def enabled(self) -> bool:
        if self._store is None:
            return False
        try:
            value = self._enabled() if callable(self._enabled) else self._enabled
        except Exception:
            return False
        return bool(value)

    def set_store(self, store) -> None:
        self._store = store

    # ── capture primitives ───────────────────────────────

    def _buffer(self, run_id: str) -> _Buffer:
        buf = self._buffers.get(run_id)
        if buf is not None:
            return buf
        self._evict_oldest()
        buf = _Buffer(run_id)
        self._buffers[run_id] = buf
        return buf

    def _evict_oldest(self) -> None:
        """Bounded process memory: the oldest run that is not being written.

        Eviction flushes instead of dropping: an overflowing process is the
        moment the evidence matters most. If no loop is running the snapshot
        is written on the next flush attempt (or lost with the process — the
        recorder never claims crash durability).
        """
        if len(self._buffers) < MAX_BUFFERS and self._total_bytes < MAX_TOTAL_BYTES:
            return
        for run_id in list(self._buffers):
            buf = self._buffers[run_id]
            if buf.task is not None and not buf.task.done():
                continue
            self._buffers.pop(run_id, None)
            self._total_bytes -= self._buffer_bytes(buf)
            if buf.events:
                self._spawn(buf)
            logger.warning("run-record buffer evicted (%s runs)", len(self._buffers) + 1)
            return

    @staticmethod
    def _buffer_bytes(buf: _Buffer) -> int:
        return sum(_bytes(e.get("payload") or {}) for e in buf.events)

    def _note_truncation(self, buf: _Buffer, key: str, amount: int = 1) -> None:
        buf.truncated = True
        buf.truncation[key] = int(buf.truncation.get(key, 0)) + int(amount)

    def _append(self, buf: _Buffer, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
            event["payload"] = payload

        omitted = self._cap_payload(kind, payload, buf)
        if omitted:
            event["truncated"] = True
            self._note_truncation(buf, "bytes_omitted", omitted)
            self._note_truncation(buf, "events_truncated")

        size = _bytes(payload)
        if size > MAX_EVENT_BYTES:
            # Nothing else can be capped safely — keep the event shape, drop
            # the body, and say so. A fake "truncated" entry inside a message
            # list would be read back as model input that never existed.
            keys = list(payload)[:20]
            event["payload"] = {"_truncated": {"bytes": size, "keys": keys}}
            event["truncated"] = True
            self._note_truncation(buf, "bytes_omitted", size)
            size = _bytes(event["payload"])

        if kind not in ("run", "finish"):
            while (len(buf.events) >= MAX_RUN_EVENTS
                   or buf.bytes + size > MAX_RUN_BYTES):
                if self._drop_one(buf):
                    continue
                # Plan B2's last resort is "stop capturing *new* raw
                # observations" — older evidence is not displaced by a
                # newer call. Retry once without this event's own body.
                if kind == "tool" and payload.get("observation") is not None:
                    dropped = _bytes(payload.get("observation"))
                    payload.pop("observation", None)
                    payload["observation_dropped"] = True
                    payload["observation_bytes"] = dropped
                    event["truncated"] = True
                    self._note_truncation(buf, "tool_observations_dropped")
                    self._note_truncation(buf, "bytes_omitted", dropped)
                    size = _bytes(payload)
                    continue
                # No evidence left to give up. Drop the *new* event rather
                # than break the run's byte ceiling; the header still says
                # the run happened and how much was omitted.
                self._note_truncation(buf, "events_dropped")
                self._note_truncation(buf, "bytes_omitted", size)
                return

        buf.seq += 1
        event["seq"] = buf.seq
        event.setdefault("created_at", _now())
        buf.events.append(event)
        buf.bytes += size
        self._total_bytes += size

    def _cap_payload(self, kind: str, payload: dict[str, Any],
                     buf: _Buffer) -> int:
        """Apply the per-kind caps; returns the number of omitted bytes."""
        omitted = 0
        if kind == "llm":
            messages = payload.get("messages")
            raw_messages = _bytes(messages) if messages is not None else 0
            if raw_messages > MAX_LLM_MESSAGES_BYTES and isinstance(messages, list):
                per_message = max(
                    1024, MAX_LLM_MESSAGES_BYTES // max(1, len(messages)))
                trimmed: list[Any] = []
                for msg in messages:
                    if not isinstance(msg, dict):
                        trimmed.append(msg)
                        continue
                    copy = dict(msg)
                    content = copy.get("content")
                    if isinstance(content, str):
                        cut, dropped = _cap_text(content, per_message)
                        copy["content"] = cut
                        omitted += dropped
                    elif isinstance(content, list):
                        # Content-block form (prompt caching): cap text parts.
                        blocks = []
                        for block in content:
                            if isinstance(block, dict) and isinstance(
                                    block.get("text"), str):
                                block = dict(block)
                                cut, dropped = _cap_text(
                                    block["text"], per_message)
                                block["text"] = cut
                                omitted += dropped
                            blocks.append(block)
                        copy["content"] = blocks
                    trimmed.append(copy)
                payload["messages"] = trimmed
                if _bytes(trimmed) > MAX_LLM_MESSAGES_BYTES:
                    # Per-message caps were not enough (many tiny messages or
                    # huge non-text parts): drop the body, keep the count.
                    payload["messages"] = []
                    payload["messages_dropped"] = True
                    payload["messages_bytes"] = raw_messages
                    omitted += raw_messages
                payload["messages_truncated_bytes"] = omitted
            if omitted:
                self._note_truncation(buf, "llm_messages_truncated")
            for key, cap in (("output", MAX_LLM_OUTPUT_BYTES),
                             ("reasoning", MAX_LLM_REASONING_BYTES)):
                cut, dropped = _cap_text(payload.get(key), cap)
                if dropped:
                    payload[key] = cut
                    payload[f"{key}_truncated_bytes"] = dropped
                    omitted += dropped
        elif kind == "tool":
            cut, dropped = _cap_text(
                payload.get("observation"), MAX_TOOL_OBSERVATION_BYTES)
            if dropped:
                payload["observation"] = cut
                payload["observation_truncated_bytes"] = dropped
                omitted += dropped
            arguments = payload.get("arguments")
            if _bytes(arguments) > MAX_TOOL_OBSERVATION_BYTES:
                cut, dropped = _cap_text(_dump(arguments), MAX_TOOL_OBSERVATION_BYTES)
                payload["arguments"] = cut
                payload["arguments_truncated_bytes"] = dropped
                omitted += dropped
        elif kind == "tool_model":
            cut, dropped = _cap_text(payload.get("content"), MAX_TOOL_MODEL_BYTES)
            if dropped:
                payload["content"] = cut
                payload["truncated_bytes"] = dropped
                omitted += dropped
        elif kind == "hitl":
            summary = payload.get("summary")
            if _bytes(summary) > MAX_EVENT_BYTES // 2:
                payload.pop("summary", None)
                payload["summary_dropped"] = True
                omitted += 1
        return omitted

    def _drop_one(self, buf: _Buffer) -> bool:
        for kind in _KIND_DROPPABLE:
            for i, event in enumerate(buf.events):
                if event.get("kind") == kind:
                    size = _bytes(event.get("payload") or {})
                    buf.events.pop(i)
                    buf.bytes -= size
                    self._total_bytes -= size
                    self._note_truncation(buf, "tool_model_dropped")
                    self._note_truncation(buf, "bytes_omitted", size)
                    return True
        for event in buf.events:
            if event.get("kind") != "llm":
                continue
            payload = event.get("payload") or {}
            messages = payload.get("messages")
            if not isinstance(messages, list) or not messages:
                continue
            size = _bytes(messages)
            payload["messages"] = []
            payload["messages_dropped"] = True
            payload["messages_bytes"] = size
            buf.bytes -= size
            self._total_bytes -= size
            event["truncated"] = True
            self._note_truncation(buf, "llm_messages_dropped")
            self._note_truncation(buf, "bytes_omitted", size)
            return True
        return False

    # ── flush ────────────────────────────────────────────

    def _spawn(self, buf: _Buffer) -> None:
        header = dict(buf.header)
        header["truncated"] = 1 if buf.truncated else 0
        header["truncation_json"] = json.dumps(
            dict(buf.truncation), ensure_ascii=False, sort_keys=True)
        events = list(buf.events)  # store write dedupes by (run_id, seq)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (sync caller): the next flush in an async context will
            # carry this snapshot. Never spawn a thread for evidence.
            return
        buf.task = loop.create_task(self._write(buf, header, events))

    async def _write(self, buf: _Buffer, header: dict[str, Any],
                     events: list[dict[str, Any]]) -> None:
        try:
            await self._store.write(header, events)
        except Exception as e:  # best-effort: the answer already went out
            logger.warning("run-record flush failed for %s: %s",
                           buf.run_id, type(e).__name__)
        finally:
            buf.task = None
            if buf.dirty and self._buffers.get(buf.run_id) is buf:
                self.schedule_flush(buf.run_id)

    def schedule_flush(self, run_id: str) -> None:
        buf = self._buffers.get(run_id)
        if buf is None:
            return
        if buf.task is not None and not buf.task.done():
            buf.dirty = True
            return
        buf.dirty = False
        self._spawn(buf)

    async def drain(self, timeout: float = 5.0) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        deadline = loop.time() + max(0.0, timeout)
        for _ in range(16):
            for buf in list(self._buffers.values()):
                if buf.dirty and (buf.task is None or buf.task.done()):
                    self.schedule_flush(buf.run_id)
            tasks = [
                buf.task for buf in list(self._buffers.values())
                if buf.task is not None and not buf.task.done()
            ]
            if not tasks:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning("run-record drain timed out with %d in flight",
                               len(tasks))
                return
            await asyncio.wait(tasks, timeout=remaining)

    def reset(self) -> None:
        for buf in list(self._buffers.values()):
            if buf.task is not None and not buf.task.done():
                buf.task.cancel()
        self._buffers.clear()
        self._total_bytes = 0

    # ── capture API ──────────────────────────────────────

    def capture_run_start(self, run_id: str, *, session_id: str = "",
                          user_id: str = "", question: str = "",
                          workflow: str = "", datasource: str = "",
                          lang: str = "") -> None:
        if not run_id or not self.enabled():
            return
        buf = self._buffer(run_id)
        buf.header.update({
            "session_id": session_id or "",
            "user_id": str(user_id or ""),
            "question": question or "",
            "workflow": workflow or "",
            "datasource": datasource or "",
        })
        if not any(e.get("kind") == "run" for e in buf.events):
            self._append(buf, {
                "kind": "run",
                "payload": {
                    "session_id": session_id or "",
                    "user_id": str(user_id or ""),
                    "question": question or "",
                    "workflow": workflow or "",
                    "datasource": datasource or "",
                    "lang": lang or "",
                },
            })
        self.schedule_flush(run_id)

    def capture_llm(self, run_id: str, *, node: str = "", model: str = "",
                    messages: Any = None, output: str = "", reasoning: str = "",
                    usage: dict | None = None, elapsed_ms: int = 0,
                    temperature: float = 0.0) -> None:
        if not run_id or not self.enabled():
            return
        buf = self._buffer(run_id)
        self._append(buf, {
            "kind": "llm",
            "node": node or "",
            "payload": {
                "model": model or "",
                "messages": _snapshot(messages) if messages is not None else [],
                "output": output or "",
                "reasoning": reasoning or "",
                "usage": usage or {},
                "elapsed_ms": int(elapsed_ms or 0),
                "temperature": temperature,
            },
        })

    def capture_llm_error(self, run_id: str, *, node: str = "",
                          model: str = "", messages: Any = None,
                          error: str = "") -> None:
        if not run_id or not self.enabled():
            return
        buf = self._buffer(run_id)
        self._append(buf, {
            "kind": "llm_error",
            "node": node or "",
            "payload": {
                "model": model or "",
                "messages": _snapshot(messages) if messages is not None else [],
                "error": error or "",
            },
        })

    def capture_tool(self, run_id: str, *, name: str, tool_call_id: str = "",
                     arguments: Any = None, observation: Any = "",
                     elapsed_ms: float = 0.0, error: Any = None) -> None:
        if not run_id or not self.enabled():
            return
        buf = self._buffer(run_id)
        self._append(buf, {
            "kind": "tool",
            "payload": {
                "name": name or "",
                "tool_call_id": tool_call_id or "",
                "arguments": _snapshot(arguments) if arguments is not None else {},
                "observation": observation,
                "elapsed_ms": int(elapsed_ms or 0),
                "error": error,
            },
        })

    def capture_tool_model(self, run_id: str, *, tool_call_id: str = "",
                           content: Any = "") -> None:
        if not run_id or not self.enabled():
            return
        buf = self._buffer(run_id)
        self._append(buf, {
            "kind": "tool_model",
            "payload": {
                "tool_call_id": tool_call_id or "",
                "content": content,
            },
        })

    def capture_kb_rev(self, run_id: str, snap: dict[str, Any] | None) -> None:
        if not run_id or not self.enabled() or not snap:
            return
        rev = str(snap.get("rev") or "")
        if not rev:
            return
        buf = self._buffer(run_id)
        buf.header["kb_rev"] = rev

    def capture_hitl(self, run_id: str, *, sql: str = "", question: str = "",
                     summary: dict[str, Any] | None = None) -> None:
        if not run_id or not self.enabled():
            return
        buf = self._buffer(run_id)
        _transition(buf, STATUS_PENDING_HITL, finished=False)
        self._append(buf, {
            "kind": "hitl",
            "payload": {
                "sql": sql or "",
                "question": question or "",
                "summary": _snapshot(summary) or {},
            },
        })
        self.schedule_flush(run_id)

    def capture_error(self, run_id: str, *, error: str = "",
                      error_info: dict | None = None) -> None:
        if not run_id or not self.enabled():
            return
        buf = self._buffer(run_id)
        _transition(buf, STATUS_ERROR, finished=True)
        buf.header["error"] = error or ""
        self._append(buf, {
            "kind": "error",
            "payload": {"error": error or "", "error_info": error_info or {}},
        })
        self.schedule_flush(run_id)

    def capture_cancel(self, run_id: str, *, reason: str = "cancelled") -> None:
        if not run_id or not self.enabled():
            return
        buf = self._buffer(run_id)
        _transition(buf, STATUS_CANCELLED, finished=True)
        buf.header["error"] = reason or "cancelled"
        self._append(buf, {"kind": "cancel", "payload": {"reason": reason}})
        self.schedule_flush(run_id)

    def capture_finish(self, run_id: str, *, final: Any = None,
                       summary: dict[str, Any] | None = None,
                       assembly: dict[str, Any] | None = None,
                       stats: dict[str, Any] | None = None,
                       model: str = "", error: str = "") -> None:
        if not run_id or not self.enabled():
            return
        buf = self._buffer(run_id)
        if summary is None:
            summary = _summary_from_final(final)
        else:
            summary = dict(summary)
        if stats:
            summary.update(stats)
        finish, omitted = _project_finish(summary)
        raw_finish = _dump(finish)
        if len(raw_finish.encode("utf-8")) > MAX_FINISH_BYTES:
            dropped: list[str] = []
            while (_bytes(finish) > MAX_FINISH_BYTES
                   and _drop_largest(finish, _FINISH_DROP_ORDER)):
                dropped.append("finish_section")
            finish["truncated_keys"] = dropped
            self._note_truncation(buf, "finish_truncated")
        finish["omitted"] = omitted
        buf.header["finish_json"] = _dump(finish)

        if assembly is not None:
            if _bytes(assembly) > MAX_ASSEMBLY_BYTES:
                assembly = {
                    "truncated": True,
                    "bytes": _bytes(assembly),
                    "keys": list(assembly)[:20],
                }
                self._note_truncation(buf, "assembly_truncated")
            buf.header["assembly_json"] = _dump(assembly)

        failed = bool(error or (final is not None
                                and getattr(final, "error", "")))
        _transition(buf, STATUS_FAILED if failed else STATUS_DONE,
                    finished=True)
        if failed:
            buf.header["error"] = error or str(getattr(final, "error", "") or "")
        if model:
            buf.header["model"] = model
        self.schedule_flush(run_id)


def _summary_from_final(final: Any) -> dict[str, Any]:
    """Minimal projection when the caller has no session summary.

    Used by direct graph drivers (``scripts/offline_eval.py``): it must not
    reach into private session internals, so it reads the public state
    fields that exist on every final ``WorkflowState``.
    """
    if final is None:
        return {}
    summary: dict[str, Any] = {}
    for key in _FINISH_KEEP:
        if key == "outcome":
            continue  # 不是 state 字段,由下面的分类器算(见 docstring)
        value = getattr(final, key, None)
        if value is not None and value != "":
            summary[key] = value
    # 循环里的 getattr 拿不到 outcome(WorkflowState 没有这个字段),而它
    # 恒非空 —— 必须显式写出,否则离线评测这条路径的 finish 会静默缺结局。
    summary["outcome"] = classify_run(
        error=str(getattr(final, "error", "") or ""),
        refusal=getattr(final, "refusal", None),
        clarification_question=str(
            getattr(final, "clarification_question", "") or ""
        ),
        hitl_status=str(getattr(final, "hitl_status", "") or ""),
    )
    return summary


def _project_finish(summary: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    finish: dict[str, Any] = {}
    for key in _FINISH_KEEP:
        if key in summary:
            finish[key] = summary[key]
    rows = finish.get("rows_preview")
    if isinstance(rows, list) and len(rows) > 50:
        finish["rows_preview"] = rows[:50]
    omitted = sorted(k for k in summary if k not in _FINISH_KEEP)
    return finish, omitted


# ── module singleton ─────────────────────────────────────

_RECORDER: RunRecorder | None = None


def configure_run_records(store=None,
                          enabled: bool | Callable[[], bool] = True) -> None:
    """Attach (or detach, with ``store=None``) the process-wide recorder."""
    global _RECORDER
    _RECORDER = RunRecorder(store, enabled) if store is not None else None


def get_run_recorder() -> RunRecorder | None:
    return _RECORDER


def is_run_records_enabled() -> bool:
    return _RECORDER is not None and _RECORDER.enabled()


def reset_run_records() -> None:
    """Test hook: cancel in-flight flushes and drop every buffer."""
    global _RECORDER
    if _RECORDER is not None:
        _RECORDER.reset()
    _RECORDER = None


async def drain_run_records(timeout: float = 5.0) -> None:
    recorder = _RECORDER
    if recorder is not None:
        await recorder.drain(timeout)


#: Capture wrappers never raise: evidence collection must not be able to
#: turn a working query into a failed one.
def _safe(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        logger.debug("run-record capture skipped: %s", type(e).__name__)


def capture_run_start(run_id: str, **kwargs) -> None:
    if _RECORDER is not None:
        _safe(_RECORDER.capture_run_start, run_id, **kwargs)


def capture_llm(run_id: str, **kwargs) -> None:
    if _RECORDER is not None:
        _safe(_RECORDER.capture_llm, run_id, **kwargs)


def capture_llm_error(run_id: str, **kwargs) -> None:
    if _RECORDER is not None:
        _safe(_RECORDER.capture_llm_error, run_id, **kwargs)


def capture_tool(run_id: str, **kwargs) -> None:
    if _RECORDER is not None:
        _safe(_RECORDER.capture_tool, run_id, **kwargs)


def capture_tool_model(run_id: str, **kwargs) -> None:
    if _RECORDER is not None:
        _safe(_RECORDER.capture_tool_model, run_id, **kwargs)


def capture_kb_rev(run_id: str, snap: dict[str, Any] | None) -> None:
    if _RECORDER is not None:
        _safe(_RECORDER.capture_kb_rev, run_id, snap)


def capture_hitl(run_id: str, **kwargs) -> None:
    if _RECORDER is not None:
        _safe(_RECORDER.capture_hitl, run_id, **kwargs)


def capture_error(run_id: str, **kwargs) -> None:
    if _RECORDER is not None:
        _safe(_RECORDER.capture_error, run_id, **kwargs)


def capture_cancel(run_id: str, **kwargs) -> None:
    if _RECORDER is not None:
        _safe(_RECORDER.capture_cancel, run_id, **kwargs)


def capture_finish(run_id: str, **kwargs) -> None:
    if _RECORDER is not None:
        _safe(_RECORDER.capture_finish, run_id, **kwargs)
