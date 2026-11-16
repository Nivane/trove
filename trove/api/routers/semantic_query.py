"""Semantic query API — standalone declarative metric/dimension surface.

``POST /v1/semantic/query``: body carries metrics/dimensions/time_grain/
filters/order_by/limit, the query compiles through the authoritative
``SemanticCompiler`` (same logical universe as the NL pipeline) and executes
read-only via the connector registry. Responses include the compiled SQL and
output columns for transparency.

Masking is part of the delivery path, not an extra: rows go through the same
``build_masker`` the graph's masking node uses (field-level ``mask``
declarations in the semantic model). Unlike the graph node — which degrades by
clearing ``state.rows`` and writing ``[ERR:MASKING]`` into the answer — this
endpoint has no answer channel, so a refusal is an HTTP error with no rows in
the body.

Non-admin users are authorized per-datasource via the standard grant surface
(``require_datasource``), then through the **same execution gate the graph uses**
(``authz.enforcer.build_authorizer``: A1 principal / A2 datasource / A3 declared
tables) plus the row/column scope engine. This route used to be the one
execution surface that bypassed the gate — and the first one a script would hit.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import (
    check_api_rate,
    get_current_user,
    get_principal,
    require_datasource,
)
from trove.api.schemas import SemanticQueryRequest
from trove.services.authz.scope import ScopeError
from trove.services.semantic_layer.query import SemanticQuery, SemanticQueryError
from trove.services.semantic_query import (
    SemanticQueryDenied,
    SemanticQueryExecutionFailed,
    SemanticQueryMaskingFailed,
    SemanticQueryTimeout,
    execute_semantic_query,
    semantic_provider,
    timeout_seconds_from_config,
)

router = APIRouter()


def _kb(request: Request):
    return request.app.state.kb


def _registry(request: Request):
    return request.app.state.connector_registry


def _provider_for(kb, datasource: str, dialect: str):
    return semantic_provider(kb=kb, datasource=datasource, dialect=dialect)


def _model_for(provider, datasource: str):
    model = provider.model()
    if model is None:
        raise HTTPException(
            status_code=404,
            detail=f"no semantic model for datasource: {datasource}",
        )
    return model


def _execute_timeout_s(request: Request) -> float:
    """执行预算(秒)—— 与图管线同一个 ``budget.timeout_ms``。

    这条路径此前**无界**:NL 管线有 wait_for + QueryTerminator,而这个直执行
    入口没有,一条慢查询能把 worker 占到进程重启。取不到配置就回到 30s 缺省
    (与 ``BudgetConfig.timeout_ms`` 同值),而不是无界。
    """
    return timeout_seconds_from_config(
        getattr(request.app.state, "config", None),
    )


async def _audit_deny(
    request: Request, user: dict, datasource: str, exc: ScopeError,
) -> None:
    """行/列拒绝的审计(``authz.deny``,best-effort)。

    details 只记授权面(reason/表名/列名),不含行值 —— 与 ``query.refused``
    同一条隐私边界。
    """
    auth = getattr(request.app.state, "auth", None)
    if auth is None or not hasattr(auth, "record_audit"):
        return
    try:
        await auth.record_audit(
            "authz.deny", user=user, method=request.method,
            path=request.url.path, status=403,
            details={
                "reason": exc.reason,
                "datasource": datasource,
                "tables": list(exc.tables),
                "columns": list(exc.columns),
            },
        )
    except Exception:
        pass


@router.get("/semantic/topics")
async def semantic_topics(
    request: Request,
    datasource: str | None = None,
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """主题域清单(用户端选择器;零 LLM、只读,与查询同一条授权面)。

    作用域列的是**生效口径**(声明 ∩ 模型当前数据集,与提问时
    ``resolve_topic`` 同一实现),并且**过期域不隐藏** —— 声明数据集全没了
    的域以 ``status="empty_scope"`` 照常列出,选择器据此显示「该域已失效」,
    而不是让它静默消失、用户带着一个选不中的旧值继续提问。

    两层授权面依次过:数据源(``require_datasource``)先行,域级收窄
    (``topic_grants``)过滤清单 —— 不可见的域**不出现在清单里**(admin 见
    全部)。注意过滤只针对可见性:过期域在**可见**的域里照常显示。
    """
    from trove.services.semantic_layer.topics import resolve_topic

    ds = await require_datasource(request, datasource, user)
    principal = await get_principal(request, user)
    kb = _kb(request)
    try:
        adapter = await _registry(request).get(ds)
        dialect = adapter.dialect() or "sqlite"
    except Exception:
        dialect = "sqlite"
    model = _model_for(_provider_for(kb, ds, dialect), ds)
    topics: list[dict[str, Any]] = []
    for t in model.topics:
        if not principal.allows_topic(ds, t.name):
            continue
        res = resolve_topic(model, t.name)
        topics.append({
            "name": t.name,
            "description": t.description,
            "synonyms": list(t.synonyms),
            "datasets": list(t.datasets),
            "scope": list(res.scope or []),  # 生效作用域(empty_scope 时为空)
            "status": res.status,  # ok | empty_scope
            "metrics": list(t.metrics),
            # 示例问句:选择器用它渲染"从这里开始问"的起始提问
            "examples": list(t.examples),
        })
    return {"datasource": ds, "topics": topics}


@router.post("/semantic/query")
async def semantic_query(
    body: SemanticQueryRequest,
    request: Request,
    user: dict = Depends(get_current_user),
    _rate: None = Depends(check_api_rate),
) -> dict[str, Any]:
    ds = await require_datasource(request, body.datasource, user)
    principal = await get_principal(request, user)
    kb = _kb(request)
    if kb is not None:
        try:
            await kb.ensure_synced(ds)
        except Exception:
            pass
    try:
        adapter = await _registry(request).get(ds)
        dialect = adapter.dialect() or "sqlite"
    except Exception:
        dialect = "sqlite"
    provider = _provider_for(kb, ds, dialect)
    model = _model_for(provider, ds)

    query = SemanticQuery(
        metrics=body.metrics or [],
        dimensions=body.dimensions or [],
        time_grain=dict(body.time_grain) if body.time_grain else None,
        filters=[f.model_dump() for f in (body.filters or [])],
        order_by=list(body.order_by or []),
        limit=body.limit,
    )
    # 编译 → 授权/scope → 有界执行 → 脱敏全部在 services.semantic_query 里,
    # 与 MCP ``query_metrics`` 走**同一条**路径。这里只把阶段化异常映射成
    # HTTP 状态:拒绝 = 403 且响应体**不含行** —— 端点没有 answer 通道,
    # 「拒绝了但照样执行」在这里与图里一样都是不可接受的失败模式。
    try:
        outcome = await execute_semantic_query(
            model=model,
            query=query,
            provider=provider,
            datasource=ds,
            dialect=dialect,
            connectors=_registry(request),
            config=getattr(request.app.state, "config", None),
            principal=principal,
            timeout_s=_execute_timeout_s(request),
        )
    except SemanticQueryError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except SemanticQueryDenied as e:
        await _audit_deny(request, user, ds, e.scope_error)
        raise HTTPException(status_code=403, detail=e.scope_error.render())
    except SemanticQueryTimeout as e:
        raise HTTPException(status_code=504, detail=str(e))
    except (SemanticQueryExecutionFailed, SemanticQueryMaskingFailed) as e:
        # 脱敏失败没有下层的 answer 通道可承载拒绝(节点会清 rows 并在回答里
        # 写 [ERR:MASKING]),所以拒绝 = HTTP 错误 + 响应体里没有行。
        raise HTTPException(status_code=500, detail=str(e))

    return outcome.to_dict()
