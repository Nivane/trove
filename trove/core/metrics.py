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
    # 记的是「闸门因何拒绝」,不是「谁被拒了」:被拒的人是高基数(用户)且审计
    # 面已有一条 query.execute + 一条 authz.deny,指标这边要回答的是
    # 「这台机器上拒绝的主因是什么」—— 单看一个总数读不出是没配主体还是表没授权,
    # 而那两条的处置方向完全不同(一个是部署没接好,一个是该加 grants)。
    AUTHZ_DENY = Counter(
        "trove_authz_deny_total",
        "Execution-time authorization denials, by reason.",
        ["reason"],
        registry=_REGISTRY,
    )
    # warn 期(§8.2)的 A3 放行**另起一条**,不并进上面那条:放行不是拒绝,并进去
    # 会让运维读到的告警率里混进一半根本没被拦的查询。这条回答的是「**量**」——
    # 多频繁、在哪个数据源上,也就是「切 enforce 会打挂多少」的分母。
    # 表**名**不在这里(见 ``record_authz_table_warn``)。
    AUTHZ_TABLE_WARN = Counter(
        "trove_authz_table_warn_total",
        "A3 table-level passes in warn mode, by datasource.",
        ["datasource"],
        registry=_REGISTRY,
    )
    # 记的是「对哪个字段用了哪种模式」,不是「改了多少行/多少值」:行数是查询
    # 的属性(已有 sql 指标),而字段名 + 模式是**声明面的事实** —— 运维要回答
    # 的是「这台机器上还有哪些列在明文进出」,按值或按行记都答不了这个。
    # 基数可控:字段名来自语义层声明(人工维护),不是用户输入。
    MASKING_APPLIED = Counter(
        "trove_masking_applied_total",
        "Field-level masking rewrites, by declared field and mode.",
        ["field", "mode"],
        registry=_REGISTRY,
    )
    # 记的是「哪个通道、哪个变量、命中哪条模式」,**不记原文** —— 原文本身
    # 就是要被隔离掉的不可信内容,进指标/日志等于把注入换个地方存着。
    # 变量名基数可控(render: 模板参数名;tool: 工具名),通道只有两条(设计稿 §5)。
    PROMPT_ISOLATION = Counter(
        "trove_prompt_isolation_total",
        "Untrusted values isolated before reaching a model, by channel/var/pattern.",
        ["channel", "var", "pattern"],
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


#: 脱敏模式的**值域**(``services/authz/masking.STRICTNESS`` 的键)。
#: 刻意不 import 那一份:``core`` 是底层,不该依赖 ``services``;而且这里要的是
#: 「计数器接受哪些标签值」,是一个观测契约 —— 服务层新增一种模式时,**这里应该
#: 有意识地跟着改一次**,而不是被自动继承。
MASKING_MODES = frozenset({"partial", "hash", "null"})


#: 拒绝原因的**值域**(``services/authz/enforcer.AuthzDecision.reason``)。
#: 与 ``MASKING_MODES`` 同一条纪律:不 import 那一份 —— ``core`` 不该依赖
#: ``services``,而且这里要的是「计数器接受哪些标签值」这个**观测契约**:
#: 服务层新增一种拒绝原因时,这里应该被有意识地改一次。
AUTHZ_DENY_REASONS = frozenset({"no_principal", "datasource", "table", "unresolved"})


#: 隔离通道的**值域**(设计稿 §5.2/§5.3):模板渲染 / 工具回喂 —— 值**进入模型
#: 上下文**的位置,有且仅有这两条(守卫测试枚举全仓 llm 调用点)。
#: ``derive`` 不是第三条入口,它记的是**派生值出生点**的处置(``screen_derived``):
#: 那里没有值进入提示词,有的是一个 LLM 产物被赋予"用户原话"的身份。
#: 与 ``MASKING_MODES`` 同一条纪律:新增通道时这里应该有意识地改一次。
ISOLATION_CHANNELS = frozenset({"render", "tool", "derive"})

#: 派生值出生点的**值域**(``llm/untrusted.screen_derived`` 的 ``site``)。
#: 这里就是"哪些地方会把 LLM 产物当作用户原话"的清单 —— 目前只有一处。
#: 站点名是代码常量(不来自模型),但仍然闭集登记:新增出生点应当**有意识地**
#: 来这里加一笔,顺带回答"我们一共开了几个这样的口子"。
ISOLATION_SITES = frozenset({"followup_rewrite"})

#: 隔离命中的**值域**(``llm/injection._PATTERNS`` 的模式名 + ``llm/untrusted``
#: 的超长保守项)。不 import 那一份 —— ``core`` 是底层,而且这里要的是
#: 「计数器接受哪些标签值」这个观测契约:模式表增删一条,这里要有意识地跟着改。
ISOLATION_PATTERNS = frozenset({
    "ignore_previous", "disregard_prior", "forget_instructions", "role_switch",
    "system_prompt", "zh_ignore", "zh_override", "oversized",
})


def record_authz_deny(reason: str) -> None:
    """记一次**执行前授权门的拒绝**(设计 §9.2 / P5)。

    域外的 reason **不记**:它只可能来自「新增了一种拒绝原因,没同步到这里」,
    而本函数是值域的定义处(同 ``record_masking_applied`` / ``record_sql_kill``)。
    放它进去等于用一次静默的基数增长换一个没人会看的序列。

    只在 ``allowed=False`` 时调用。warn 期(§8.2)的 A3 放行走
    ``record_authz_table_warn``。
    """
    if not _HAVE_CLIENT or reason not in AUTHZ_DENY_REASONS:
        return
    try:
        AUTHZ_DENY.labels(reason=reason).inc()
    except Exception as e:
        logger.debug("authz deny metric record failed: %s", e)


def record_authz_table_warn(datasource: str) -> None:
    """记一次 **warn 期放行的 A3 表级判定**(设计 §8.2)。

    只在「命中未声明表、但档位是 warn 所以放行」时调用 —— 也就是
    ``decision.allowed and decision.narrowed_tables`` 的那一格。

    **表名不进标签**:``record_sql_budget_decision`` 已立此纪律,并且点名了表名
    (基数无限)。§8.2 要的「哪些表会被拒」是**名字**,名字在审计行里
    (``authz.table_warn`` 的 ``tables`` 字段)—— 那是行数据,随库增长无所谓;
    放进标签则是把监控系统自己拖垮(同路由用模板而不是原始 URL)。这条计数器
    回答**量**:切 enforce 会打挂多少、打挂谁家的库。

    空源名兜底成 ``"default"``(同 ``record_sql_degraded``):没解析出源名不是
    漏记的理由 —— 漏了这一笔,分子就少了,而它是决策的分子。
    """
    if not _HAVE_CLIENT:
        return
    try:
        AUTHZ_TABLE_WARN.labels(datasource=datasource or "default").inc()
    except Exception as e:
        logger.debug("authz table warn metric record failed: %s", e)


def record_masking_applied(field: str, mode: str) -> None:
    """记一次**实际发生**的字段改写(设计 §9.2 / §5.5)。

    空字段名/域外模式**不记**:前者只可能来自一个坏报告,后者不是一次改写 ——
    尤其 ``bypass``:它意味着**有人持 scope 读走了原文**,是审计面的事实
    (§6.3 的 ``masking.bypass`` 进 ``audit_log``),不是脱敏计数。混进同一个
    系列,「有多少列在脱敏」会被决策次数污染,而审计要回答的「谁看过原文」
    计数器又答不了。守卫放在本函数而不在调用点:值域是这个计数器定义的
    (同 ``record_sql_kill`` 与 ``""`` 的纪律)。
    """
    if not _HAVE_CLIENT or not field or mode not in MASKING_MODES:
        return
    try:
        MASKING_APPLIED.labels(field=field, mode=mode).inc()
    except Exception as e:
        logger.debug("masking metric record failed: %s", e)


def record_prompt_isolation(channel: str, var: str, pattern: str) -> None:
    """记一次**实际发生**的外部值隔离(设计稿 §5.4)。

    空变量名 / 域外通道 / 域外模式**不记**:后两者只可能来自「新增了一条通道或
    模式,没同步到这里」,而本函数是值域的定义处(同 ``record_authz_deny``)。
    ``channel="derive"`` 时 ``var`` 是站点名,同样要在 ``ISOLATION_SITES`` 里 ——
    否则「哪些出生点在处置派生值」这个问题就没有一个可读的答案。
    不记原文 —— 原文正是要被隔离掉的东西。
    """
    if (not _HAVE_CLIENT or not var or channel not in ISOLATION_CHANNELS
            or pattern not in ISOLATION_PATTERNS
            or (channel == "derive" and var not in ISOLATION_SITES)):
        return
    try:
        PROMPT_ISOLATION.labels(channel=channel, var=var, pattern=pattern).inc()
    except Exception as e:
        logger.debug("prompt isolation metric record failed: %s", e)


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
