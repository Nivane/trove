"""Langfuse SDK observability — full-trajectory tracing.

Architecture (SDK single channel — no litellm callbacks, no double
recording):

  - LangGraph runs with langfuse.langchain.CallbackHandler in
    config["callbacks"] → one trace per question, one span per node
    (subgraph spans nest).
  - The gateway records each LLM completion as a generation nested in
    the current node span (contextvar propagation), including
    reasoning_content (the CoT field of reasoning models).
  - Non-LLM steps (SQL execution) record tool spans via record_span().

Everything is a silent no-op when Langfuse credentials are not in the
environment, so tests and CI run untouched.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

from trove.core.logging import get_logger

logger = get_logger(__name__)

_client = None  # lazy singleton


def langfuse_trace_id(run_id: str) -> str:
    """Langfuse 合法 trace id = 32 位小写 hex。

    run_id 是带连字符的 uuid4;SDK 校验 trace id 必须是 32 lowercase hex
    (带连字符会被忽略,导致确定性 trace 定位失效)。去掉连字符即合法。
    """
    return (run_id or "").replace("-", "").lower()


def langfuse_enabled() -> bool:
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
    )


def get_client():
    """Lazy Langfuse client, None when disabled or initialization fails."""
    global _client
    if not langfuse_enabled():
        return None
    if _client is None:
        try:
            from langfuse import Langfuse
            _client = Langfuse()
        except Exception as e:
            logger.warning("Langfuse client init failed: %s", e)
            _client = None
    return _client


def build_callback_handler(trace_id: str | None = None):
    """CallbackHandler for LangGraph config["callbacks"], None when disabled.

    ``trace_id`` pins a deterministic trace id (= run_id), so post-run
    updates (verdict summary, user scores) can target the same trace.
    None keeps the SDK's auto-generated trace id (legacy single-handler).
    """
    if not langfuse_enabled():
        return None
    try:
        from langfuse.langchain import CallbackHandler
        if trace_id:
            return CallbackHandler(trace_context={"trace_id": langfuse_trace_id(trace_id)})
        return CallbackHandler()
    except Exception as e:
        logger.warning("Langfuse CallbackHandler init failed: %s", e)
        return None


# verdict → 数值评分(便于 Langfuse 按答案质量筛选 trace)
VERDICT_SCORE = {"OK": 1.0, "RETRY": 0.5, "EMPTY": 0.2}


def record_run_finish(run_id: str, summary: dict[str, Any]) -> None:
    """Attach a run's final verdict/timings/tokens to its trace (no-op disabled).

    Uses the deterministic trace_id (= run_id) set by build_callback_handler:
    - create_score: numeric verdict for analytics (OK=1.0 / RETRY=0.5 / 0.0).
    - create_event: the run summary (sql/verdict/elapsed/tokens) on the trace.
    Both are safe when the trace never ran (cache-hit-only paths create the
    trace implicitly). Failures are swallowed — observability never breaks runs.
    """
    client = get_client()
    if client is None:
        return
    trace_id = langfuse_trace_id(run_id)
    try:
        verdict = str(summary.get("verdict") or "")
        client.create_score(
            trace_id=trace_id,
            name="trove.verdict",
            value=VERDICT_SCORE.get(verdict, 0.0),
            comment=verdict,
            metadata={"question": summary.get("question", "")[:300]},
        )
    except Exception as e:
        logger.debug("Run-verdict score failed: %s", e)
    try:
        output = {k: v for k, v in summary.items() if k not in ("rows", "rows_preview", "chart_option")}
        client.create_event(
            trace_context={"trace_id": trace_id},
            name="run.summary",
            output=_cap(output, OBSERVATION_TRUNCATE),
        )
    except Exception as e:
        logger.debug("Run-summary event failed: %s", e)


def _cap(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit]
    if isinstance(value, dict):
        return {k: _cap(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cap(v, limit) for v in value[:10]]
    return value


def record_user_score(run_id: str, vote: int, comment: str = "") -> None:
    """Write a user up/down vote as a Langfuse score on the run's trace.

    Links the rating (``POST /v1/kb/ratings``) back to the trace that
    produced the answer — closed-loop observability. No-op without Langfuse.
    """
    client = get_client()
    if client is None:
        return
    try:
        client.create_score(
            trace_id=langfuse_trace_id(run_id),
            name="trove.user_rating",
            value=float(vote),
            data_type="NUMERIC",
            comment=(comment or None),
        )
    except Exception as e:
        logger.debug("User score failed: %s", e)


@contextmanager
def record_span(name: str, input: Any = None, metadata: Any = None, trace_context: dict[str, Any] | None = None):
    """Open a nested span (no-op without Langfuse); yields the span or None.

    Callers may update the yielded span: `if span: span.update(output=...)`.
    `metadata` is passed through to the observation (session grouping).
    ``trace_context`` (e.g. {"trace_id": run_id}) pins the observation to a
    specific trace — used for cache-hit roots that skip the graph.

    Langfuse SDK v4 removed start_as_current_span/generation — observations
    with an explicit type are the current API.

    Body exceptions always pass through untouched — span recording must
    never mask the wrapped operation's real error (a second yield inside
    except would make contextlib raise "generator didn't stop after
    throw()" and hide the original exception).
    """
    import sys as _sys

    client = get_client()
    if client is None:
        yield None
        return
    try:
        cm = client.start_as_current_observation(
            as_type="span", name=name, input=input, metadata=metadata or {},
            trace_context=trace_context,
        )
        span = cm.__enter__()
    except Exception as e:
        logger.debug("Span recording failed (%s): %s", name, e)
        yield None
        return
    try:
        yield span
    finally:
        # 收尾只在 finally 里做:body 异常原样穿透,teardown 失败静默
        try:
            cm.__exit__(*_sys.exc_info())
        except Exception as e:
            logger.debug("Span teardown failed (%s): %s", name, e)


OBSERVATION_TRUNCATE = 2000  # 工具观测进 langfuse 的截断上限


def record_tool_call(
    name: str,
    arguments: dict[str, Any] | None = None,
    observation: str = "",
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
    max_obs_len: int = OBSERVATION_TRUNCATE,
) -> None:
    """Record one tool call as a langfuse span (no-op without Langfuse).

    Nests under the current node span via context propagation — agent-loop
    tools (probe/check/validate/search_values) land inside the gen_sql
    node span. `observation` is truncated (runlog keeps full fidelity);
    a tool error is recorded at level=ERROR with the message.
    """
    client = get_client()
    if client is None:
        return
    try:
        if error:
            output: dict[str, Any] = {"error": error[:max_obs_len]}
            level, status_message = "ERROR", error[:max_obs_len]
        else:
            output = {"observation": observation[:max_obs_len]}
            level, status_message = "DEFAULT", None
        with client.start_as_current_observation(
            as_type="span",
            name=f"tool.{name}",
            input={"arguments": arguments},
            output=output,
            level=level,
            status_message=status_message,
            metadata=metadata or {},
        ):
            pass
    except Exception as e:
        logger.debug("Tool span recording failed (%s): %s", name, e)
