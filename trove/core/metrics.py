"""Process metrics — in-process Prometheus registry (single-process serve).

Counters/histograms for the hot paths worth watching in production: HTTP
traffic (middleware in api/app.py), LLM calls (hooks in llm/gateway.py),
datasource SQL executions (hooks in datasource/registry.py), and the execution
cost track — budget verdicts / degraded executions / kill outcomes (hooks in
workflow/nodes/execute_sql.py). Exposed at `GET /v1/metrics` in the standard
Prometheus text format.

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
    # ── 执行画像的成本轨(设计 §9.2 / R1)──────────────────
    #
    # 三条都**只在 `workflow/nodes/execute_sql.py` 记**,不在 BudgetService /
    # describe_cost 里记:同一套 estimate/decide 在「生成前问价」时也会跑一遍,
    # 在服务层记会把**问价的**和**真跑了的**混成一个数 —— 问价不碰数据库,真跑
    # 了的那条可能打爆生产库,两者的运维含义相反。节点记才等于「这条查询落库了」。
    SQL_BUDGET_DECISIONS = Counter(
        "trove_sql_budget_decisions_total",
        "Cost-track verdicts at the execution node, by datasource/source/verdict.",
        ["datasource", "source", "verdict"],
        registry=_REGISTRY,
    )
    # 降级**执行**。为什么单开一个而不从 verdict 推:verdict="degrade" 混着两种
    # 成因 —— (1) 一点估算依据都没有(保守预算,I2,R1 要的正是这一个);(2) 画像档
    # 估出这是张大表(P2 接的依据,降级恰恰是因为**有**依据)。混在一起会把「有依据、
    # 按大表处置」记成「没有护栏」;后者单独看得用
    # decisions{source="metadata",verdict="degrade"}。另一个差别是时点:decisions
    # 在**判定**时记(含没跑成的 reject),本计数器只记**执行**(没落库的不算)。
    SQL_DEGRADED = Counter(
        "trove_sql_degraded_total",
        "Executions with no cost estimate (conservative budget), by datasource.",
        ["datasource"],
        registry=_REGISTRY,
    )
    # 注意这个计数器回答的是「终止指令**发出去**了没有」,不是「查询停下来了没有」:
    # kill_sent 只到「交给了驱动、驱动没报错」为止(见 terminate.KILL_SENT)。
    SQL_KILL = Counter(
        "trove_sql_kill_total",
        "Kill attempt outcome after a query timed out, by datasource/result.",
        ["datasource", "result"],
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


def record_sql_budget_decision(datasource: str, source: str, verdict: str) -> None:
    """一次成本判定(设计 §9.2 / R1)。``verdict`` 三档都记 —— 含没跑成的 reject。

    两个枚举作为标签进得来,是因为它们的值域是**闭的**(``CostEstimate.source`` /
    ``BudgetDecision.verdict`` 各有三个取值);SQL 文本、表名、错误原文一律不进
    标签 —— 基数是无限的,进去就是把监控系统自己拖垮(与路由用模板而不是原始
    URL 同一条纪律)。
    """
    if not _HAVE_CLIENT:
        return
    try:
        SQL_BUDGET_DECISIONS.labels(
            datasource=datasource or "default", source=source, verdict=verdict,
        ).inc()
    except Exception as e:  # metrics must never break the request path
        logger.debug("sql budget decision metric record failed: %s", e)


def record_sql_degraded(datasource: str) -> None:
    """一条**没有估算依据**的执行(设计 §9.2 / R1 / I2)。

    与 ``verdict="degrade"`` 不是同一件事 —— 差别见 ``SQL_DEGRADED`` 上的注释。
    """
    if not _HAVE_CLIENT:
        return
    try:
        SQL_DEGRADED.labels(datasource=datasource or "default").inc()
    except Exception as e:
        logger.debug("sql degraded metric record failed: %s", e)


def record_sql_kill(datasource: str, result: str) -> None:
    """超时后那一次终止尝试的结果(设计 §9.2 / §10 / I4)。

    ``result`` 的值域见 ``services/sql/terminate.py``:``kill_sent`` /
    ``kill_unsupported`` / ``kill_failed``。

    ``result=""`` **不记**:那是「**没试过**」(没装终止器 / 这条查询没超时),不是
    一种终止结果。记一个空串等于让「我们根本没接这条轨」混进结果分布,而它与
    「这个方言不支持终止」对运维的含义相反 —— 前者是我们漏接了一步,后者是库
    不给这个能力。守卫放在本函数而不是调用点:值域是这个计数器定义的,任何调用
    方都不该有办法往一个「没有结果」的结果里记一笔(同 ``degraded`` 与 ``""``、
    ``as_of_basis`` 与「没查过」的三态纪律)。
    """
    if not _HAVE_CLIENT or not result:
        return
    try:
        SQL_KILL.labels(datasource=datasource or "default", result=result).inc()
    except Exception as e:
        logger.debug("sql kill metric record failed: %s", e)


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
