"""Observability wiring — Langfuse via litellm callbacks.

Enable in agent.yml:

    agent:
      observability:
        tracing:
          enabled: true

and provide Langfuse credentials via environment (.env):
    LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST

Every litellm call then lands in Langfuse with its prompt/completion
visible; per-call metadata (node, session_id, question) is passed via
the gateway's metadata parameter so traces can be grouped by session
and pipeline stage (CoT/plan/SQL all visible step by step).
"""

from __future__ import annotations

from trove.core.config import TracingConfig


def configure_tracing(tracing: TracingConfig) -> None:
    """Check tracing readiness (SDK single-channel mode).

    Recording happens via trove.llm.observability (LangGraph callback
    handler + generation/tool spans) whenever Langfuse credentials are
    present in the environment — no litellm callbacks are registered
    (that would double-record every call)."""
    if not tracing.enabled:
        return

    from trove.llm.observability import langfuse_enabled, get_logger as _unused
    from trove.core.logging import get_logger as _get_logger

    _logger = _get_logger(__name__)
    if langfuse_enabled():
        _logger.info("Tracing enabled: Langfuse credentials detected")
    else:
        _logger.warning(
            "Tracing enabled in config but LANGFUSE_PUBLIC_KEY/SECRET_KEY "
            "are not set — no traces will be recorded"
        )
