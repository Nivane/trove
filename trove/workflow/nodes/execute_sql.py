"""ExecuteSQL node — runs generated SQL against the datasource.

Cancellation is handled by asyncio task cancellation (CancelledError
propagates through the graph); no explicit cancellation-event checks.

Node shape: `async def execute_sql(state: WorkflowState) -> dict`
returns a partial state update.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.i18n import L
from trove.core.logging import get_logger
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.limits import get_result_limits
from trove.services.errors import is_transient, tag_error
from trove.llm.observability import record_span
from trove.workflow.state import WorkflowState, budget_exhausted

logger = get_logger(__name__)


def make_execute_sql(
    connectors: ConnectorRegistry | None = None,
    timeout_ms: int = 30000,
    max_retries: int = 10,
    lineage=None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the execute_sql node bound to a connector registry.

    Args:
        connectors: Registry used to run SQL (None → error update).
        timeout_ms: Query timeout in milliseconds.
        max_retries: Shared correction budget — execution failures feed
            back to gen_sql for regeneration while retry_count < max_retries;
            once exhausted, failures degrade gracefully via state.error.
        lineage: Optional LineageService — successful queries are recorded
            as downstream (consumer) lineage facts for the datasource.

    Returns:
        Async node function taking WorkflowState and returning a partial update.
    """

    async def execute_sql(state: WorkflowState) -> dict[str, Any]:
        # Upstream node failed — pass through without running
        if state.error:
            return {}

        if not state.sql:
            return {"error": "No SQL to execute — SQL generation did not produce a query."}

        if connectors is None:
            return {"error": "No datasource registry available."}

        result = None
        timeout_s = timeout_ms / 1000.0
        retryable = _TRANSIENT_RETRIES  # 瞬时连接抖动的小重试预算(同一条 SQL)
        retry_backoff_s = _TRANSIENT_BACKOFF_S
        while True:
            try:
                with record_span("tool.execute_sql", input=state.sql) as span:
                    result = await asyncio.wait_for(
                        connectors.execute(state.sql, state.datasource or None),
                        timeout=timeout_s,
                    )
                    if span is not None:
                        span.update(output={"row_count": result.row_count})
                break
            except asyncio.TimeoutError:
                # 超时未必是 SQL 的问题(慢查询/连接抖动)——残余重试预算内再试一次
                if retryable > 0:
                    retryable -= 1
                    await asyncio.sleep(retry_backoff_s)
                    retry_backoff_s = min(retry_backoff_s * 2, 4.0)
                    continue
                return _execution_failure(
                    state,
                    "[ERR:SQL_TIMEOUT] "
                    + L(
                        state.lang,
                        f"查询超时（{timeout_ms}ms）",
                        f"Query timed out after {timeout_ms}ms",
                    ),
                    max_retries,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 瞬态连接错误(断连/不可达/协议重置)与 SQL 错误(语法/列不存在)
                # 判别:前者重跑同一 SQL 大概率恢复,后者重跑必死——只在瞬时
                # 错误上烧小重试,SQL 错误直接喂回错误反馈(同旧语义)。
                # 反馈文本一律带 [ERR:<class>] 前缀,供 analyze_error 预分流。
                if retryable > 0 and is_transient(e):
                    retryable -= 1
                    await asyncio.sleep(retry_backoff_s)
                    retry_backoff_s = min(retry_backoff_s * 2, 4.0)
                    continue
                return _execution_failure(
                    state, tag_error(str(e), context="sql"), max_retries,
                )

        # 血缘捕获:成功执行的查询记录为消费方(downstream)事实。
        # 失败永远不记录(重试轮的正确 SQL 由最终成功的一次独占)。
        if lineage is not None:
            try:
                ds = state.datasource or connectors.default_name or ""
                if ds:
                    await lineage.record_query(state.sql, ds, state.dialect)
            except Exception as e:  # 血缘失败绝不阻断查询链路
                logger.warning("lineage record failed: %s", e)

        result_limits = get_result_limits()
        return {
            "columns": result.columns,
            # 查询结果上限(管理台可配,默认 1000 行):防止超大结果集撑爆
            # 内存/传输;row_count 保留真实总数用于展示与提示。
            "rows": result.rows[:result_limits.max_rows],
            "row_count": result.row_count,
            "execution_time_ms": result.execution_time_ms,
            "error_feedback": "",  # success clears previous feedback
        }

    return execute_sql


# 瞬时连接错误的小重试预算(同一条 SQL,不烧 LLM 生成预算)。
# 针对本地/远程 MySQL 抖动:断连、不可达、协议重置等瞬态错误重跑大概率
# 恢复;SQL 自身错误(语法/缺列)重跑必死,不做无谓的 sleep。
_TRANSIENT_RETRIES = 2
_TRANSIENT_BACKOFF_S = 0.5


def _is_transient(exc: BaseException) -> bool:
    """判别瞬时连接类异常(重试可恢复) vs SQL 错误(重试无意义)。

    委托给确定性错分器(services/errors):只认可 DS_TRANSIENT / RATE_LIMIT
    两类连接层故障,语法/缺列/权限等一律 False(保守——绝不把语法错误
    当瞬态去重试)。基于异常类型 + 错误文本双重信号。
    """
    return is_transient(exc)


def _execution_failure(
    state: WorkflowState, message: str, max_retries: int,
) -> dict[str, Any]:
    """Feed the error back to gen_sql, or degrade when the budget is spent."""
    if budget_exhausted(state.retry_count, max_retries):
        return {"error": message}
    return {
        "error_feedback": message,
        "retry_count": state.retry_count + 1,
        "correction_history": [message],
        # 清掉执行产物并标记"本轮未执行":rows/columns 可能是上一轮成功的
        # 残留——analyze_error 的回归检查用 row_count == -1 区分执行错误
        # (结果集签名无意义)与执行后的规则/裁决失败(签名可比)。
        "columns": [],
        "rows": [],
        "row_count": -1,
    }
