"""Deterministic semantic-query execution — one path for every entry point.

``POST /v1/semantic/query`` and the MCP ``query_metrics`` tool must answer
with the same numbers under the same guarantees. Keeping the sequence
(compile → authorize/scope → bounded execute → mask) in one module is the
only way that stays true: a second hand-written copy of this pipeline is how
the HTTP route once became the one execution surface that bypassed the gate.

The module sits at the ``services`` layer and imports both siblings
(``authz`` and ``semantic_layer``); neither imports back, so there is no
cycle. It owns no state: every call is bounded, read-only, and audited via
the same counters the graph uses.

Failure vocabulary (callers map these to their own transport):

* :class:`SemanticQueryDenied` — the execution gate or scope engine refused
  (carries the :class:`~trove.services.authz.scope.ScopeError`).
* :class:`SemanticQueryTimeout` — the query exceeded the execution budget.
* :class:`SemanticQueryExecutionFailed` — the datasource returned an error.
* :class:`SemanticQueryMaskingFailed` — masking was required but refused;
  no rows may leave the process.
* :class:`~trove.services.semantic_layer.query.SemanticQueryError` — the
  query itself is not answerable by the model (compile stage, unchanged).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trove.core.metrics import record_authz_deny, record_masking_applied
from trove.services.authz.enforcer import build_authorizer
from trove.services.authz.masking import MaskingError, build_masker
from trove.services.authz.policy import Principal, principal_to_wire
from trove.services.authz.scope import (
    ScopeError,
    authorize_and_scope,
    has_column_denials,
)
from trove.services.semantic_layer.models import SemanticModel
from trove.services.semantic_layer.provider import SemanticLayerProvider
from trove.services.semantic_layer.query import SemanticQuery, build_and_compile

logger = logging.getLogger(__name__)

#: 未配置预算时的执行上限(与 ``BudgetConfig.timeout_ms`` 同值)。
DEFAULT_EXECUTION_TIMEOUT_S = 30.0


class SemanticQueryDenied(Exception):
    """The authorize/scope gate refused this principal."""

    def __init__(self, scope_error: ScopeError) -> None:
        self.scope_error = scope_error
        super().__init__(scope_error.render())


class SemanticQueryTimeout(Exception):
    """The query exceeded its execution budget."""

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        super().__init__(f"execution timed out after {timeout_s:g}s")


class SemanticQueryExecutionFailed(Exception):
    """The datasource rejected or failed the statement."""

    def __init__(self, cause: BaseException) -> None:
        self.cause = cause
        super().__init__(f"execution failed: {cause}")


class SemanticQueryMaskingFailed(Exception):
    """Masking had to run and refused — no rows may be returned."""

    def __init__(self, cause: BaseException) -> None:
        self.cause = cause
        super().__init__(f"masking refused: {cause}")


@dataclass(frozen=True)
class SemanticQueryOutcome:
    """A completed semantic query, ready to render on either transport."""

    sql: str
    data_scope: dict[str, Any]
    columns: list[str]
    datasets: list[str]
    version: str
    rows: list[list[Any]]
    masking_applied: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            # scope 后的 SQL(行过滤是派生表重写)—— 返回编译原串会让调用方
            # 以为执行的与看到的是同一条,而它们可能差一个 ``WHERE``。
            "sql": self.sql,
            "data_scope": self.data_scope,
            "columns": self.columns,
            "datasets": self.datasets,
            "version": self.version,
            "rows": self.rows,
            "row_count": len(self.rows),
            "masking_applied": self.masking_applied,
        }


def timeout_seconds_from_config(config: Any) -> float:
    """Execution budget in seconds; never unbounded.

    取不到配置就回到 30s 缺省(与 ``BudgetConfig.timeout_ms`` 同值),而不是
    无界 —— 一条慢查询能把 worker 占到进程重启。
    """
    raw = getattr(getattr(config, "budget", None), "timeout_ms", 0)
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        ms = 0
    return (ms if ms > 0 else int(DEFAULT_EXECUTION_TIMEOUT_S * 1000)) / 1000.0


def semantic_provider(
    *, kb: Any, datasource: str, dialect: str,
) -> SemanticLayerProvider:
    """Build the live provider for one datasource (KB file is the truth)."""
    kb_path = kb.semantics_path(datasource) if kb is not None else None
    return SemanticLayerProvider(
        directory=Path.cwd() / ".trove" / "semantic" / datasource,
        datasource=datasource,
        dialect=dialect,
        kb_semantics_path=kb_path,
    )


async def execute_semantic_query(
    *,
    model: SemanticModel,
    query: SemanticQuery,
    provider: SemanticLayerProvider,
    datasource: str,
    dialect: str,
    connectors: Any,
    config: Any,
    principal: Principal,
    timeout_s: float,
) -> SemanticQueryOutcome:
    """Compile, authorize, execute and mask one semantic query.

    Every stage is the same object the NL pipeline uses: ``build_and_compile``
    for the SQL, ``build_authorizer`` + ``authorize_and_scope`` for the gate
    (declared RLS already lives in the compiled SQL, so
    ``include_declared_rls=False``), ``build_masker`` for delivery.

    Exceptions are typed by stage so callers never have to string-match an
    error message to decide the HTTP/MCP status.
    """
    compiled = build_and_compile(model, query, dialect=dialect)

    try:
        scoped = authorize_and_scope(
            compiled["sql"],
            authorizer=build_authorizer(config, provider),
            principal=principal,
            model=model,
            datasource=datasource,
            dialect=dialect,
            default=getattr(connectors, "default_name", None),
            include_declared_rls=False,
        )
    except ScopeError as e:
        record_authz_deny(e.reason)
        raise SemanticQueryDenied(e) from e

    try:
        # 有界执行:超时取消走与图管线同一条解栈(适配器的 CancelledError
        # 分支会发 interrupt/KILL,服务端查询真的停下)。
        result = await asyncio.wait_for(
            connectors.execute(scoped.sql, datasource), timeout=timeout_s,
        )
    except TimeoutError as e:
        raise SemanticQueryTimeout(timeout_s) from e
    except Exception as e:
        raise SemanticQueryExecutionFailed(e) from e

    rows: list[list[Any]] = list(result.rows)
    masking_applied: dict[str, Any] | None = None
    # 构造走 build_masker(与节点/select 预览同一份规则);``None`` = 部署级
    # 关闭,整段跳过 —— 开关语义只在 build_masker 里读。
    apply_masking = build_masker(semantic_layer=provider, config=config)
    if apply_masking is not None:
        try:
            rows, masking_applied = apply_masking(
                rows, compiled["columns"],
                principal=principal_to_wire(principal),
                sql=scoped.sql,
            )
        except MaskingError as e:
            # 失败方向取严:降级放行即泄漏。调用方必须返回错误且**不含行**。
            raise SemanticQueryMaskingFailed(e) from e
        for field, mode in (masking_applied.get("fields") or {}).items():
            record_masking_applied(field, str(mode))

    return SemanticQueryOutcome(
        sql=scoped.sql,
        data_scope={
            "row_filters": list(scoped.row_filters),
            "rewritten": scoped.rewritten,
            "column_policy": has_column_denials(principal, datasource),
        },
        columns=compiled["columns"],
        datasets=compiled["datasets"],
        version=compiled["version"],
        rows=rows,
        masking_applied=masking_applied,
    )
