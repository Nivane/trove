"""Request-ID / run-ID log correlation: contextvar + logging filter.

Verifies every log record carries request_id and run_id so stderr lines
can be correlated to an HTTP request and to a question run (traces.jsonl /
runs/{run_id}.log use the same run_id).
"""

from __future__ import annotations

import contextvars
import logging

from trove.core.request_id import (
    RequestIdFilter,
    request_id_var,
    run_id_var,
)


def _make_record(msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="trove.test", level=logging.INFO,
        pathname=__file__, lineno=1, msg=msg, args=(), exc_info=None,
    )


class TestRunIdLogging:
    def test_defaults_dash_when_unset(self):
        rec = _make_record("x")
        RequestIdFilter().filter(rec)
        assert rec.request_id == "-"
        assert rec.run_id == "-"

    def test_run_id_from_contextvar(self):
        token = run_id_var.set("run-123")
        try:
            rec = _make_record("x")
            RequestIdFilter().filter(rec)
            assert rec.run_id == "run-123"
        finally:
            run_id_var.reset(token)

    def test_both_ids_combined(self):
        tok_rq = request_id_var.set("req-1")
        tok_run = run_id_var.set("run-2")
        try:
            rec = _make_record("x")
            RequestIdFilter().filter(rec)
            assert rec.request_id == "req-1"
            assert rec.run_id == "run-2"
        finally:
            request_id_var.reset(tok_rq)
            run_id_var.reset(tok_run)

    def test_contextvar_scoped_to_context(self):
        """run_id 不跨任务泄漏:子任务各自持自己的上下文。"""
        base = run_id_var.get()
        token = run_id_var.set("run-a")
        inner = contextvars.copy_context()
        got = inner.run(run_id_var.get)
        run_id_var.reset(token)
        assert got == "run-a"
        assert run_id_var.get() == base
