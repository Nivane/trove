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

# 字段知识与读取规则住在 core(观测层也用它,而 tracing 不得依赖 llm);
# 本模块 re-export,调用方的既有 import 路径不变。
from trove.core.token_usage import CACHE_FIELDS, cache_hit_tokens, cache_suffix

__all__ = [
    "CACHE_FIELDS",
    "add",
    "cache_hit_tokens",
    "cache_suffix",
    "get",
    "pop",
    "reset",
]

_usage: dict[str, dict[str, int]] = {}


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
    for f in CACHE_FIELDS:
        value = usage.get(f)
        if value is not None:  # 键存在 = 测量过(报 0 也留:0 命中 ≠ 没测量)
            bucket[f] = bucket.get(f, 0) + int(value)


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