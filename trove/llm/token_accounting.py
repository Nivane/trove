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

# 需要聚合的 token 字段(prompt/completion 之外,含 prompt 缓存命中)。
# 公开常量:eval/replay 侧另存同序副本(该文件不 import 任何 trove 模块),
# 由测试钉住两份相等 —— 缓存指标跨引擎必须同源,否则门与基线各算各的。
CACHE_FIELDS = (
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
    for f in CACHE_FIELDS:
        value = usage.get(f)
        if value is not None:  # 键存在 = 测量过(报 0 也留:0 命中 ≠ 没测量)
            bucket[f] = bucket.get(f, 0) + int(value)


def cache_hit_tokens(bucket: dict[str, Any] | None) -> int | None:
    """命中缓存的 prompt token 数;两键同时可读时**取一不求和**。

    ``cached_tokens``(litellm 规范拼写,DeepSeek/OpenAI 系)优先,缺失时
    退 ``cache_read_input_tokens``(Anthropic 原生)——litellm 对 DeepSeek
    会同时设出这两个拼写(同一命中数),求和会翻倍。``cache_creation_
    input_tokens`` 是"写缓存"不计入命中。两键都不在 = 未测量 → None。
    """
    if not bucket:
        return None
    if bucket.get("cached_tokens") is not None:
        return int(bucket["cached_tokens"])
    if bucket.get("cache_read_input_tokens") is not None:
        return int(bucket["cache_read_input_tokens"])
    return None


def cache_suffix(bucket: dict[str, Any] | None) -> str:
    """人类可读缓存后缀 ``cache N(x%)``(命中/prompt);未测量或分母缺 → ""。"""
    hit = cache_hit_tokens(bucket)
    if hit is None or not bucket:
        return ""
    prompt = int(bucket.get("prompt") or 0)
    if prompt <= 0:
        return ""
    return f"cache {hit}({hit / prompt:.0%})"


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