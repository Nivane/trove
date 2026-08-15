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


def build_callback_handler():
    """CallbackHandler for LangGraph config["callbacks"], None when disabled."""
    if not langfuse_enabled():
        return None
    try:
        from langfuse.langchain import CallbackHandler
        return CallbackHandler()
    except Exception as e:
        logger.warning("Langfuse CallbackHandler init failed: %s", e)
        return None


@contextmanager
def record_span(name: str, input: Any = None):
    """Open a nested span (no-op without Langfuse); yields the span or None.

    Callers may update the yielded span: `if span: span.update(output=...)`.
    """
    client = get_client()
    if client is None:
        yield None
        return
    try:
        with client.start_as_current_span(name=name, input=input) as span:
            yield span
    except Exception as e:
        logger.debug("Span recording failed (%s): %s", name, e)
        yield None
