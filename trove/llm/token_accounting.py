"""Per-run LLM token accounting.

A process-level accumulator keyed by run_id: every real (non-mock) LLM
call that carries a run_id in its gateway metadata contributes its
prompt/completion/total token counts (gateway._record_local_call). The
SessionManager pops the tally when it emits the "done" event so the REPL
and frontend can show per-question token usage, and so the tally never
leaks between runs in the same process.

Mirrors the tracing.local pattern (process-level global); the test
conftest autouse fixture resets it to guarantee isolation.
"""

from __future__ import annotations

from typing import Any

_usage: dict[str, dict[str, int]] = {}

# 需要聚合的 token 字段(prompt/completion 之外,含 prompt 缓存命中)
_CACHE_FIELDS = (
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cached_tokens",
)


def add(run_id: str, usage: dict[str, Any]) -> None:
    """Add one call's token usage to a run's running total."""
    if not run_id:
        return
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or 0)
    if not (prompt or completion or total):
        return  # provider omitted usage (mock/legacy) — stay silent
    bucket = _usage.setdefault(run_id, {"prompt": 0, "completion": 0, "total": 0})
    bucket["prompt"] += prompt
    bucket["completion"] += completion
    bucket["total"] += total
    for f in _CACHE_FIELDS:
        v = int(usage.get(f) or 0)
        if v:
            bucket[f] = bucket.get(f, 0) + v


def get(run_id: str) -> dict[str, int] | None:
    """Accumulated usage for a run without removing it (None when missing)."""
    if not run_id:
        return None
    return _usage.get(run_id)


def pop(run_id: str) -> dict[str, int] | None:
    """Return and clear the usage for a run (None when missing)."""
    if not run_id:
        return None
    return _usage.pop(run_id, None)


def reset() -> None:
    """Clear all tallies (test isolation / teardown)."""
    _usage.clear()