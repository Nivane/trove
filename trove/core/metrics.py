"""Process metrics — in-process Prometheus registry (single-process serve).

Counters/histograms for the three hot paths worth watching in production:
HTTP traffic (middleware in api/app.py), LLM calls (hooks in llm/gateway.py),
and datasource SQL executions (hooks in datasource/registry.py). Exposed at
`GET /v1/metrics` in the standard Prometheus text format.

Labels are deliberately low-cardinality (route template, not raw URL —
the middleware uses the matched route path so `/v1/sessions/{id}` stays
one series). All record helpers are no-ops if the client library is
missing, so metrics never take down the request path.
"""

from __future__ import annotations

import time
from typing import Any

from trove.core.logging import get_logger

logger = get_logger(__name__)

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    _HAVE_CLIENT = True
except ImportError:  # pragma: no cover — dependency declared in pyproject
    _HAVE_CLIENT = False

_REGISTRY: Any = None
if _HAVE_CLIENT:
    from prometheus_client import CollectorRegistry
    _REGISTRY = CollectorRegistry()

    HTTP_REQUESTS = Counter(
        "trove_http_requests_total",
        "HTTP requests handled by the API, by method/route/status.",
        ["method", "path", "status"],
        registry=_REGISTRY,
    )
    HTTP_DURATION = Histogram(
        "trove_http_request_duration_seconds",
        "HTTP request wall-clock duration (including streaming setup).",
        ["method", "path"],
        registry=_REGISTRY,
    )
    HTTP_INFLIGHT = Gauge(
        "trove_http_requests_inflight",
        "HTTP requests currently being handled.",
        registry=_REGISTRY,
    )
    LLM_CALLS = Counter(
        "trove_llm_calls_total",
        "LLM provider attempts, by provider/model/status (success|error).",
        ["provider", "model", "status"],
        registry=_REGISTRY,
    )
    LLM_DURATION = Histogram(
        "trove_llm_call_duration_seconds",
        "LLM provider attempt wall-clock duration.",
        ["provider", "model"],
        registry=_REGISTRY,
    )
    SQL_QUERIES = Counter(
        "trove_sql_queries_total",
        "Datasource SQL executions, by datasource/status (success|error).",
        ["datasource", "status"],
        registry=_REGISTRY,
    )
    SQL_DURATION = Histogram(
        "trove_sql_query_duration_seconds",
        "Datasource SQL execution wall-clock duration.",
        ["datasource"],
        registry=_REGISTRY,
    )
    SQL_CACHE_HITS = Counter(
        "trove_sql_cache_hits_total",
        "ConnectorRegistry result-cache hits, by datasource.",
        ["datasource"],
        registry=_REGISTRY,
    )


def _short_model(model: str) -> str:
    """Collapse long model strings (revisions/suffixes) to keep series count low."""
    return model.split("/")[-1] if "/" in model else model


def _provider_of(model: str) -> str:
    """Best-effort provider from a litellm-style model id ('openai/gpt-4o' → 'openai')."""
    return model.split("/")[0] if "/" in model else "default"


def record_http(method: str, path: str, status: int, duration_s: float) -> None:
    if not _HAVE_CLIENT:
        return
    try:
        HTTP_REQUESTS.labels(method=method, path=path, status=str(status)).inc()
        HTTP_DURATION.labels(method=method, path=path).observe(duration_s)
    except Exception as e:  # metrics must never break the request path
        logger.debug("http metric record failed: %s", e)


def http_inflight_inc() -> None:
    if not _HAVE_CLIENT:
        return
    try:
        HTTP_INFLIGHT.inc()
    except Exception as e:
        logger.debug("http inflight inc failed: %s", e)


def http_inflight_dec() -> None:
    if not _HAVE_CLIENT:
        return
    try:
        HTTP_INFLIGHT.dec()
    except Exception as e:
        logger.debug("http inflight dec failed: %s", e)


def record_llm_call(model: str, status: str, duration_s: float) -> None:
    if not _HAVE_CLIENT:
        return
    try:
        provider = _provider_of(model)
        LLM_CALLS.labels(
            provider=provider, model=_short_model(model), status=status,
        ).inc()
        LLM_DURATION.labels(provider=provider, model=_short_model(model)).observe(
            duration_s
        )
    except Exception as e:
        logger.debug("llm metric record failed: %s", e)


def record_sql(datasource: str, status: str, duration_s: float) -> None:
    if not _HAVE_CLIENT:
        return
    try:
        SQL_QUERIES.labels(datasource=datasource or "default", status=status).inc()
        SQL_DURATION.labels(datasource=datasource or "default").observe(duration_s)
    except Exception as e:
        logger.debug("sql metric record failed: %s", e)


def record_sql_cache_hit(datasource: str) -> None:
    if not _HAVE_CLIENT:
        return
    try:
        SQL_CACHE_HITS.labels(datasource=datasource or "default").inc()
    except Exception as e:
        logger.debug("sql cache metric record failed: %s", e)


def render_metrics() -> bytes:
    """Render the registry in Prometheus text format (empty payload if absent)."""
    if not _HAVE_CLIENT:
        return b""
    return generate_latest(_REGISTRY)


class MetricsTimer:
    """单调时钟计时器:elapsed_s() 返回自构造以来的秒数(计量用,非业务计时)。"""

    def __init__(self):
        self._start = time.monotonic()

    def elapsed_s(self) -> float:
        return time.monotonic() - self._start
