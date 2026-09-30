"""Observability wiring — Langfuse via SDK single channel.

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

The two conditions are an AND, and each answers a different question:

  - credentials present?  → *can* we record (nothing to send with otherwise)
  - ``tracing.enabled``?  → *may* we record (the compliance switch)

``enabled: false`` therefore wins over credentials: it suppresses every
recording entry point (``observability.langfuse_enabled`` gates them all),
which is the whole point of having the switch — someone who wants their
users' questions to stop leaving the machine must be able to stop it from
config alone. ``enabled: true`` does not *force* recording (there is
nothing to record without credentials); it means "do not suppress".
"""

from __future__ import annotations

from trove.core.config import TracingConfig


def configure_tracing(tracing: TracingConfig) -> None:
    """Apply the config-side tracing gate, then report readiness.

    Recording happens via trove.llm.observability (LangGraph callback
    handler + generation/tool spans) when credentials are present **and**
    the config gate is open — no litellm callbacks are registered (that
    would double-record every call)."""
    from trove.llm.observability import langfuse_enabled, set_tracing_suppressed

    set_tracing_suppressed(not tracing.enabled)
    if not tracing.enabled:
        return

    from trove.core.logging import get_logger as _get_logger

    _logger = _get_logger(__name__)
    if langfuse_enabled():
        _logger.info("Tracing enabled: Langfuse credentials detected")
    else:
        _logger.warning(
            "Tracing enabled in config but LANGFUSE_PUBLIC_KEY/SECRET_KEY "
            "are not set — no traces will be recorded"
        )
