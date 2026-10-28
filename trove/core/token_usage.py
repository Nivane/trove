"""Pure prompt-cache token vocabulary shared by accounting and tracing.

These three names are field knowledge, not runtime state: the cache-field
tuple and the two read-only helpers say how provider usage buckets spell
cache hits. They live in ``core`` because both ``llm`` (the accumulator) and
``tracing`` (the run log) need them, and ``tracing`` must not depend on
``llm``.

``trove.llm.token_accounting`` re-exports them, so callers keep their import
path. ``trove/eval/replay.py`` deliberately keeps its own copy (that file
imports no trove module at all); ``tests/eval/test_replay.py`` pins the two
copies equal.
"""

from __future__ import annotations

from typing import Any

# 需要聚合的 token 字段(prompt/completion 之外,含 prompt 缓存命中)。
# 公开常量:eval/replay 侧另存同序副本(该文件不 import 任何 trove 模块),
# 由测试钉住两份相等 —— 缓存指标跨引擎必须同源,否则门与基线各算各的。
CACHE_FIELDS = (
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cached_tokens",
)


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
