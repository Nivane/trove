"""Request-ID propagation — contextvar + logging filter.

The API middleware (trove/api/app.py) sets/clears ``request_id_var`` per
request; the filter below attaches the value to every log record emitted
inside that request so stderr lines correlate with HTTP traffic. Outside
a request the id renders as "-".

``run_id_var`` is the per-question twin: runlog.create_tracer sets it at
run start so every stderr line emitted while a question executes carries
the same run_id that the trace store / runs/{run_id}.log use — you can
correlate a log line to a trace without grepping request ids.
"""

from __future__ import annotations

import contextvars
import logging

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "run_id", default=None
)


def get_request_id() -> str | None:
    """Current request id (None outside a request context)."""
    return request_id_var.get()


def get_run_id() -> str | None:
    """Current run id (None outside a question run)."""
    return run_id_var.get()


class RequestIdFilter(logging.Filter):
    """Logging filter exposing the contextvar as %(request_id)s."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        record.run_id = run_id_var.get() or "-"
        return True
