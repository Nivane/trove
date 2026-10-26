"""一次运行的账面：生命周期 + 代价，**纯派生、绝不写回**。

输入是 :meth:`trove.storage.run_record_store.RunRecordStore.get` 返回的
``{header, events}``；输出是闭形 dict（顶层键与 ``COVERAGE_CODES`` 由机检
冻结）。同一份输入永远给出同一份输出——账本是读出来的视图，不新建账本表、
不进 SSE、不进用户回放、不做前端 UI；写回会让同一段历史在不同时刻给出不同
的数。

三条纪律：

* **只统计可观测事实**：LLM 调用次数/模型、tokens、工具与 SQL 探测次数、
  耗时、缓存命中。不做金额估算——单价是配置事实，不是运行事实。
* **没测到 ≠ 0**：缺事实一律 ``None`` + 覆盖码。``0`` 是一个结论（「测了，
  是零」），``None`` 是「不知道」；把后者洗成前者就是这份账本最坏的失败
  方式（与 ``analysis.stats`` / ``result_limits`` 同一条纪律）。
* **不复制敏感载荷**：不输出问题原文、SQL 原文、行预览。账本是审计聚合面。

本模块零 I/O、零墙钟、零 ``trove.llm`` / ``trove.services`` / 工作流 import；
唯一依赖是 :mod:`trove.core.run_outcome`（结局词汇不另立分类器）。
"""
from __future__ import annotations

from typing import Any, Mapping

from trove.core.run_outcome import record_outcome

#: 账本形状版本（生产者 schema 版本，描述产物形状而非内容；与 P2
#: ``analysis_payload`` 同一条版本纪律）。增删顶层键或覆盖码都要推进。
LEDGER_VERSION = 1

#: 覆盖码闭集：账本**说得出**的「这份记录看不到什么」。每条都对应一种
#: 真实的缺席，不是分类标签——新增一条 = 一种新的不可见性进入审计面。
COVERAGE_CODES: tuple[str, ...] = (
    # 没有事件（旧记录 / 只写过 header）—— 计数是 None 不是 0
    "no_events",
    # 整条事件被丢弃（truncation.events_dropped > 0）→ 计数只是下界
    "events_dropped",
    # finish 超限裁过 → execution 等节可能缺席
    "finish_truncated",
    # running / pending_hitl：还没有结局可言
    "unfinished",
    # 终态但没有 finish（error / cancel 路径）
    "no_finish",
    # 库里的 status 不在闭集（旧 / 坏数据）—— 读端不 500
    "unknown_status",
    # 没有任何 token 事实
    "tokens_unmeasured",
    # finish 缺 token_usage，用 llm 事件求和（下界）
    "tokens_from_events",
    # 运行记录不含查询预算快照（执行过 SQL 才构成缺席）
    "budget_not_recorded",
    # 没有装配报告
    "context_not_recorded",
    # 连接器缓存命中没有 per-run 归属（执行过 SQL 才构成缺席）
    "connector_cache_not_recorded",
)

_UNFINISHED = ("running", "pending_hitl")

#: 缓存命中 token 的两个拼写（litellm 规范 / Anthropic 原生）。两键同时
#: 可读时**取一不求和**——它们描述同一次命中，求和会翻倍。
_CACHED_KEYS = ("cached_tokens", "cache_read_input_tokens")

#: finish.execution_evidence 进账本的固定键。``budget`` / ``source`` /
#: ``as_of_basis`` 等配置/口径元数据刻意不进——账本记运行事实，不复制配置。
_EXECUTION_KEYS = (
    "verdict", "reason", "degraded", "estimated_rows", "estimated_bytes",
    "limit_applied", "terminated", "kill",
)

#: 「SQL 工具」事件名 → 账本字段。工具级可观测事实，不冒充连接器层执行数。
_SQL_TOOLS = {
    "probe_query": "probes",
    "check_result": "checks",
    "validate_sql": "validations",
}


def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    """事件载荷；缺席/坏形状 → 空映射（读端不因一条脏事件 500）。"""
    value = event.get("payload")
    return value if isinstance(value, Mapping) else {}


def _opt_int(value: Any) -> int | None:
    """可读整数；``None`` / 布尔 / 不可解析 → ``None``（不猜成 0）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stamped(truncation: Mapping[str, Any], key: str) -> bool:
    value = _opt_int(truncation.get(key))
    return bool(value and value > 0)


def _cached_tokens(bucket: Mapping[str, Any]) -> int | None:
    """择一规则与 ``token_accounting.cache_hit_tokens`` 同源（内联三行，
    不 import ``trove.llm``——账本不能把 LLM 层拖进核心依赖）。"""
    for key in _CACHED_KEYS:
        value = _opt_int(bucket.get(key))
        if value is not None:
            return value
    return None


def _event_usage(
    llm_events: list[Mapping[str, Any]],
) -> dict[str, int | None] | None:
    """从 llm 事件的 ``usage`` 求和；**没有任何一条报过 usage → None**。

    逐字段求和：某字段从未出现 → ``None``（不拿 0 冒充）；出现则缺失视为 0。
    缓存命中 token 先按拼写择一（全轮任一事件报过 ``cached_tokens`` 就只认
    这个拼写），与 finish 侧同一规则。
    """
    usages = [_payload(e).get("usage") for e in llm_events]
    usages = [u for u in usages if isinstance(u, Mapping) and u]
    if not usages:
        return None
    out: dict[str, int | None] = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        seen = [u for u in usages if _opt_int(u.get(field)) is not None]
        out[field] = sum(_opt_int(u.get(field)) or 0 for u in seen) if seen else None
    cached: int | None = None
    for key in _CACHED_KEYS:
        seen = [u for u in usages if _opt_int(u.get(key)) is not None]
        if seen:
            cached = sum(_opt_int(u.get(key)) or 0 for u in seen)
            break
    out["cached"] = cached
    return out


def _tokens(
    finish: Mapping[str, Any], llm_events: list[Mapping[str, Any]],
) -> tuple[dict[str, int | None], str | None, list[str]]:
    """tokens 块 + 来源 + 覆盖码。finish 是持久事实，事件求和只是下界。"""
    usage = finish.get("token_usage")
    if isinstance(usage, Mapping) and usage:
        return (
            {
                "prompt": _opt_int(usage.get("prompt")),
                "completion": _opt_int(usage.get("completion")),
                "total": _opt_int(usage.get("total")),
                "cached": _cached_tokens(usage),
            },
            "finish",
            [],
        )
    from_events = _event_usage(llm_events)
    if from_events is not None:
        return (
            {
                "prompt": from_events["prompt_tokens"],
                "completion": from_events["completion_tokens"],
                "total": from_events["total_tokens"],
                "cached": from_events["cached"],
            },
            "events",
            ["tokens_from_events"],
        )
    return (
        {"prompt": None, "completion": None, "total": None, "cached": None},
        None,
        ["tokens_unmeasured"],
    )


def _execution(finish: Mapping[str, Any]) -> dict[str, Any] | None:
    evidence = finish.get("execution_evidence")
    if not isinstance(evidence, Mapping):
        return None
    return {key: evidence.get(key) for key in _EXECUTION_KEYS}


def build_run_ledger(record: Mapping[str, Any]) -> dict[str, Any]:
    """``{header, events}`` → 闭形账本。不修改输入。"""
    header = record.get("header") or {}
    if not isinstance(header, Mapping):
        header = {}
    events = record.get("events") or []
    if not isinstance(events, list):
        events = []
    # 坏事件(非映射)不算「有事件」:它们提供不了任何可计数的东西。
    events = [e for e in events if isinstance(e, Mapping)]
    finish = header.get("finish") or {}
    if not isinstance(finish, Mapping):
        finish = {}
    truncation = header.get("truncation") or {}
    if not isinstance(truncation, Mapping):
        truncation = {}

    status = str(header.get("status") or "")
    try:
        outcome: str | None = record_outcome(status, finish)
        unknown_status = False
    except ValueError:
        # 写端闭集抛错；读端相反——一条旧/坏记录不该让管理端 500。
        outcome = None
        unknown_status = True

    coverage: list[str] = []
    if unknown_status:
        coverage.append("unknown_status")
    if not events:
        coverage.append("no_events")
    if _stamped(truncation, "events_dropped"):
        coverage.append("events_dropped")
    if _stamped(truncation, "finish_truncated"):
        coverage.append("finish_truncated")
    if status in _UNFINISHED:
        coverage.append("unfinished")
    if status not in _UNFINISHED and not finish:
        coverage.append("no_finish")

    llm_events = [e for e in events if e.get("kind") == "llm"]
    llm_errors = sum(1 for e in events if e.get("kind") == "llm_error")
    tool_events = [e for e in events if e.get("kind") == "tool"]

    models = {str(_payload(e).get("model") or "") for e in llm_events}
    models.discard("")
    if finish.get("model"):
        models.add(str(finish["model"]))

    def _elapsed(items: list[Mapping[str, Any]]) -> int | None:
        if not items:
            return None
        return sum(_opt_int(_payload(e).get("elapsed_ms")) or 0 for e in items)

    tokens, tokens_source, token_codes = _tokens(finish, llm_events)
    coverage.extend(token_codes)

    by_name: dict[str, int] = {}
    tool_errors = 0
    for e in tool_events:
        payload = _payload(e)
        name = str(payload.get("name") or "")
        if name:
            by_name[name] = by_name.get(name, 0) + 1
        if payload.get("error") is not None:
            tool_errors += 1

    sql_counts: dict[str, int | None] = {}
    for name, field in _SQL_TOOLS.items():
        sql_counts[field] = by_name.get(name, 0) if events else None

    executed_sql = bool(str(finish.get("sql") or "").strip())
    if finish and executed_sql:
        if _execution(finish) is None:
            coverage.append("budget_not_recorded")
        # 连接器层缓存命中没有 per-run 归属：这不是本记录缺字段，而是账本
        # 在这一维上的结构性盲点——执行过 SQL 的轮次才会遇见它。
        coverage.append("connector_cache_not_recorded")
    if finish and not (isinstance(header.get("assembly"), Mapping)
                       and header.get("assembly")):
        coverage.append("context_not_recorded")

    cached = finish.get("cached") if finish else None
    ledger = {
        "version": LEDGER_VERSION,
        "run_id": str(header.get("run_id") or ""),
        "status": status,
        "outcome": outcome,
        "started_at": header.get("started_at"),
        "finished_at": header.get("finished_at"),
        "elapsed_ms": _opt_int(finish.get("total_elapsed_ms")),
        "llm": {
            "calls": len(llm_events) if events else None,
            "errors": llm_errors if events else None,
            "models": sorted(models),
            "elapsed_ms": _elapsed(llm_events),
            "tokens": tokens,
            "tokens_source": tokens_source,
        },
        "tools": {
            "calls": len(tool_events) if events else None,
            "errors": tool_errors if events else None,
            "by_name": dict(sorted(by_name.items())) if events else None,
            "elapsed_ms": _elapsed(tool_events),
        },
        "sql": {
            **sql_counts,
            "row_count": _opt_int(finish.get("row_count")),
            "retry_count": _opt_int(finish.get("retry_count")),
            "execution": _execution(finish),
        },
        "cache": {"answer_cached": bool(cached) if cached is not None else None},
        "verdict": finish.get("verdict") or None,
        "coverage": sorted(set(coverage)),
    }
    return ledger
