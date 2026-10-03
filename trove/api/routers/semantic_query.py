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
(``require_datasource``).
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import (
    check_api_rate,
    get_current_user,
    get_principal,
    require_datasource,
)
from trove.api.schemas import SemanticQueryRequest
from trove.core.metrics import record_masking_applied
from trove.services.authz.masking import MaskingError, build_masker
from trove.services.authz.policy import principal_to_wire
from trove.services.semantic_layer.query import (
    SemanticQuery,
    SemanticQueryError,
    build_and_compile,
)

router = APIRouter()


def _kb(request: Request):
    return request.app.state.kb


def _registry(request: Request):
    return request.app.state.connector_registry


def _provider_for(kb, datasource: str, dialect: str):
    from pathlib import Path

    from trove.services.semantic_layer.provider import SemanticLayerProvider

    return SemanticLayerProvider(
        directory=Path.cwd() / ".trove" / "semantic" / datasource,
        datasource=datasource,
        dialect=dialect,
        kb_semantics_path=kb.semantics_path(datasource),
    )


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
    config = getattr(request.app.state, "config", None)
    raw = getattr(getattr(config, "budget", None), "timeout_ms", 0)
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        ms = 0
    return (ms if ms > 0 else 30_000) / 1000.0


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
    """
    from trove.services.semantic_layer.topics import resolve_topic

    ds = await require_datasource(request, datasource, user)
    kb = _kb(request)
    try:
        adapter = await _registry(request).get(ds)
        dialect = adapter.dialect() or "sqlite"
    except Exception:
        dialect = "sqlite"
    model = _model_for(_provider_for(kb, ds, dialect), ds)
    topics: list[dict[str, Any]] = []
    for t in model.topics:
        res = resolve_topic(model, t.name)
        topics.append({
            "name": t.name,
            "description": t.description,
            "synonyms": list(t.synonyms),
            "datasets": list(t.datasets),
            "scope": list(res.scope or []),  # 生效作用域(empty_scope 时为空)
            "status": res.status,  # ok | empty_scope
            "metrics": list(t.metrics),
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
    try:
        compiled = build_and_compile(model, query, dialect=dialect)
    except SemanticQueryError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        # 有界执行:超时取消走与图管线同一条解栈(适配器的 CancelledError
        # 分支会发 interrupt/KILL,服务端查询真的停下),然后折成 504 ——
        # 「我们等不下去了」与「语句本身出错」(500)是两回事。
        result = await asyncio.wait_for(
            _registry(request).execute(compiled["sql"], ds),
            timeout=_execute_timeout_s(request),
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"execution timed out after {_execute_timeout_s(request):g}s",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"execution failed: {e}")

    rows: list[list[Any]] = result.rows
    masking_applied: dict[str, Any] | None = None
    # 构造走 build_masker(与节点/select 预览同一份规则);``None`` = 部署级
    # 关闭,整段跳过 —— 开关语义只在 build_masker 里读。
    apply_masking = build_masker(
        semantic_layer=provider,
        config=getattr(request.app.state, "config", None),
    )
    if apply_masking is not None:
        try:
            principal = await get_principal(request, user)
            rows, masking_applied = apply_masking(
                rows, compiled["columns"],
                principal=principal_to_wire(principal),
                sql=compiled["sql"],
            )
        except MaskingError as e:
            # 没有下层的 answer 通道可承载拒绝(节点会清 rows 并在回答里写
            # [ERR:MASKING]),所以拒绝 = HTTP 错误 + 响应体里没有行。失败
            # 方向取严:降级放行即泄漏。
            raise HTTPException(status_code=500, detail=f"masking refused: {e}")
        for field, mode in (masking_applied.get("fields") or {}).items():
            record_masking_applied(field, str(mode))

    return {
        "sql": compiled["sql"],
        "columns": compiled["columns"],
        "datasets": compiled["datasets"],
        "version": compiled["version"],
        "rows": rows,
        "row_count": len(rows),
        "masking_applied": masking_applied,
    }
