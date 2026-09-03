"""Graph-state checkpoint inspection & replay (admin checkpoint management).

Reads the LangGraph checkpointer directly — the same one the graphs are
compiled with (``main.create_app_components`` exposes it as the
``checkpointer`` component). Three capabilities:

- ``list_thread``: checkpoint timeline of one session (thread_id = session_id)
- ``get_checkpoint``: full state snapshot at one checkpoint
- ``replay_from``: resume the graph from an arbitrary checkpoint (time-travel)

The checkpointer is duck-typed: anything exposing ``alist`` / ``aget_tuple``
works (AsyncSqliteSaver, AsyncPostgresSaver, MemorySaver, or test fakes).

Checkpoint ordering: ``alist`` yields newest-first (matches the maintenance
service's "``latest[0]`` = newest" assumption).
"""

from __future__ import annotations

from typing import Any

# Internal nodes that never denote a real pipeline node in version-seen traces.
_SKIP_NODES = {"__input__", "__start__", "__end__"}

# 列表视图的"一眼状态":快照里最有诊断价值的字段(其余走详情接口)。
_SUMMARY_FIELDS = [
    "question",
    "plan",
    "sql",
    "verdict",
    "reason",
    "retry_count",
    "error",
    "error_feedback",
    "hitl_status",
    "row_count",
    "semantics",
    "final_response",
]


def _json_safe(value: Any) -> Any:
    """JSON-safe conversion: non-primitive values degrade to str (never raise)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def node_for(tuple_) -> str:
    """Derive the node that produced this checkpoint from ``versions_seen``.

    LangGraph's checkpoint carries ``versions_seen`` = the cumulative set of
    nodes that have observed channels so far; the last real node is the one
    whose execution wrote this checkpoint.
    """
    vs = tuple_.checkpoint.get("versions_seen") or {}
    nodes = [n for n in vs.keys() if n not in _SKIP_NODES]
    return nodes[-1] if nodes else ""


def _state_summary(tuple_) -> dict[str, Any]:
    cv = tuple_.checkpoint.get("channel_values") or {}
    return {
        field: _json_safe(cv.get(field))
        for field in _SUMMARY_FIELDS
        if field in cv
    }


def _entry(tuple_) -> dict[str, Any]:
    """Public shape of one checkpoint row (list + detail share this)."""
    return {
        "checkpoint_id": tuple_.config["configurable"].get("checkpoint_id", ""),
        "thread_id": tuple_.config["configurable"].get("thread_id", ""),
        "ts": tuple_.checkpoint.get("ts", ""),
        "step": tuple_.metadata.get("step"),
        "source": tuple_.metadata.get("source", ""),
        "run_id": tuple_.metadata.get("run_id", ""),
        "node": node_for(tuple_),
        "state": _state_summary(tuple_),
    }


async def list_thread(checkpointer, thread_id: str, limit: int = 100) -> list[dict]:
    """Checkpoint timeline of one session (newest first, capped by limit).

    Ordering is by ``checkpoint_id`` (LangGraph's time-based UUID, guaranteed
    monotonically increasing within a thread) — the raw ``ts`` string is not
    strictly ordered across backends, while the id is.
    """
    out: list[dict] = []
    try:
        async for tuple_ in checkpointer.alist(
            {"configurable": {"thread_id": thread_id}}, limit=limit
        ):
            out.append(_entry(tuple_))
    except Exception:
        return []
    out.sort(key=lambda c: c["checkpoint_id"], reverse=True)
    return out[:limit]


async def get_checkpoint(
    checkpointer, thread_id: str, checkpoint_id: str,
) -> dict | None:
    """Full state snapshot at one checkpoint (None = not found)."""
    try:
        tuple_ = await checkpointer.aget_tuple({
            "configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id},
        })
    except Exception:
        return None
    if tuple_ is None:
        return None
    return {
        **_entry(tuple_),
        "parent_checkpoint_id": (
            (tuple_.parent_config or {}).get("configurable", {}).get("checkpoint_id")
        ),
        "state_full": _json_safe(tuple_.checkpoint.get("channel_values") or {}),
    }


async def replay_from(
    graph,
    thread_id: str,
    checkpoint_id: str,
    workflow: str,
) -> dict[str, Any]:
    """Resume the graph from an arbitrary checkpoint (time-travel replay).

    Replays from the checkpoint forward on the same thread; the final state
    is returned as a summary. Re-entering a HITL interrupt yields
    ``hitl_status="pending"`` so the standard resume flow can continue it.
    """
    from trove.workflow.state import WorkflowState

    config = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_id": checkpoint_id,
        },
    }
    result = await graph.ainvoke(None, config)
    final = WorkflowState.model_validate(
        {k: v for k, v in result.items() if k != "__interrupt__"}
    )
    summary = {
        "session_id": final.session_id,
        "run_id": final.run_id,
        "question": final.question,
        "sql": final.sql,
        "row_count": final.row_count,
        "verdict": final.verdict,
        "reason": final.reason,
        "error": final.error,
        "semantics": final.semantics,
        "insights": final.insights,
        "conclusion": final.conclusion,
        "hitl_status": final.hitl_status,
        "final_response": final.final_response,
        "matched_tables": list(final.matched_tables),
    }
    return summary
