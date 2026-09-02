"""FastAPI application factory.

`create_app(components)` wraps the dict returned by
`main.create_app_components` (session_manager, catalog_service,
connector_registry, kb, ...) into a FastAPI app. Each component is
exposed on `app.state.<name>` for the routers; tests build the
components dict with mocks.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from trove.api.routers import catalog, chat, facts, kb, semantic
from trove.core.errors import DatasourceError, SessionError
from trove.core.logging import get_logger
from trove.core.metrics import (
    MetricsTimer,
    http_inflight_dec,
    http_inflight_inc,
    record_http,
    render_metrics,
)
from trove.core.request_id import request_id_var

logger = get_logger(__name__)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9.\-_]{1,64}$")
_HEALTH_PING_TIMEOUT_S = 2.0


async def _purge_auth(app: FastAPI) -> None:
    """Auth-store hygiene: purge expired tokens + stale login attempts.

    Best-effort and never blocks the sweep loop; skipped silently when
    no auth service (or a NullAuth stand-in) is present.
    """
    auth = getattr(app.state, "auth", None)
    if auth is None or not hasattr(auth, "purge_expired_tokens"):
        return
    try:
        tokens = await auth.purge_expired_tokens()
        attempts = await auth.purge_old_login_attempts()
        if tokens or attempts:
            logger.info(
                "[auth] hygiene purge: %s expired tokens, %s login attempts",
                tokens, attempts,
            )
    except Exception as e:
        logger.warning("[auth] hygiene purge failed: %s", e)


async def _periodic_sweep(app: FastAPI) -> None:
    """Background loop: run retention sweep every sweep_interval_hours."""
    config = getattr(app.state, "config", None)
    interval_hours = getattr(
        getattr(config, "retention", None), "sweep_interval_hours", 0
    )
    if interval_hours <= 0:
        return
    while True:
        await asyncio.sleep(interval_hours * 3600)
        try:
            stats = await app.state.maintenance.run_all()
            logger.info("[maintenance] periodic sweep: %s", stats)
        except Exception as e:
            logger.warning("[maintenance] periodic sweep failed: %s", e)
        # 记忆生命周期:过期/归档清理(情景记忆、偏好草稿、用户事实、
        # 检索日志) + schema 漂移检测。静默降级,不阻断 maintenance。
        memory = getattr(app.state, "memory", None)
        if memory is not None and getattr(memory, "enabled", False):
            try:
                mem_stats = await memory.run_lifecycle()
                if any(mem_stats.get("purged", {}).values()) or mem_stats.get("drift"):
                    logger.info("[memory] lifecycle sweep: %s", mem_stats)
            except Exception as e:
                logger.warning("[memory] lifecycle sweep failed: %s", e)
        await _purge_auth(app)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    maintenance = getattr(app.state, "maintenance", None)
    sweep_task: asyncio.Task | None = None
    startup_task: asyncio.Task | None = None
    purge_task = asyncio.create_task(_purge_auth(app))
    if maintenance is not None:
        # 启动 sweep 不阻塞 serve:后台任务,内部自包异常防护
        async def _startup_sweep() -> None:
            try:
                stats = await maintenance.run_all()
                logger.info("[maintenance] startup sweep: %s", stats)
            except Exception as e:
                logger.warning("[maintenance] startup sweep failed: %s", e)

        startup_task = asyncio.create_task(_startup_sweep())
        sweep_task = asyncio.create_task(_periodic_sweep(app))
    try:
        yield
    finally:
        for task in (sweep_task, startup_task, purge_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


def create_app(components: dict) -> FastAPI:
    """Build the FastAPI app from a components dict (see main.py)."""
    app = FastAPI(title="Trove API", version="0.1.0", docs_url="/v1/docs", lifespan=_lifespan)
    for name, value in components.items():
        setattr(app.state, name, value)

    # Auth: real service → mount /v1/auth (+ /v1/admin when present);
    # missing → NullAuth fallback so embedded/stray callers keep working
    # (every request runs as synthetic local admin, loud one-time warning).
    from trove.api.deps import NullAuth
    from trove.api.routers import admin as admin_router
    from trove.api.routers import auth as auth_router

    auth = components.get("auth")
    if auth is None:
        auth = NullAuth()
        app.state.auth = auth
        logger.warning(
            "AUTH DISABLED — no auth service in components; "
            "requests run as synthetic local admin"
        )
    if not isinstance(auth, NullAuth):
        app.include_router(auth_router.router, prefix="/v1")
        app.include_router(admin_router.router, prefix="/v1")

    app.include_router(chat.router, prefix="/v1")
    app.include_router(catalog.router, prefix="/v1")
    app.include_router(kb.router, prefix="/v1")
    app.include_router(facts.router, prefix="/v1")
    app.include_router(semantic.router, prefix="/v1")

    @app.exception_handler(SessionError)
    async def _session_error(request: Request, exc: SessionError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(DatasourceError)
    async def _datasource_error(request: Request, exc: DatasourceError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    # ── Middleware ──────────────────────────────────────────────
    # 装饰器后加的在外层:request-id 最外(所有路径都带响应头/日志关联),
    # metrics 内层。两个中间件都不抛错——度量失败只降级为 debug 日志。

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next):
        """Accept a caller X-Request-ID (bounded charset) or mint one;
        echo it back and attach it to every log record in this request."""
        header = request.headers.get("x-request-id")
        rid = header if header and _REQUEST_ID_RE.match(header) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            request_id_var.reset(token)

    @app.middleware("http")
    async def _metrics_middleware(request: Request, call_next):
        """Count every request + wall-clock duration (route template, not raw
        URL, as the path label — keeps series cardinality bounded)."""
        timer = MetricsTimer()
        http_inflight_inc()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            # 路由匹配发生在中间件内层(call_next 里),scope["route"]
            # 要在这里读;未匹配/中间件早退时归入 "unmatched"。
            route = request.scope.get("route")
            path = getattr(route, "path", None) or "unmatched"
            http_inflight_dec()
            record_http(request.method, path, status, timer.elapsed_s())

    @app.get("/v1/health")
    async def health(request: Request) -> JSONResponse:
        """Liveness + real dependency checks.

        200: 存储可用(数据源/LLM 异常降级为 "degraded",仍 200 — 进程活着
        但不完整);503: 内部存储不可达。LLM 只报配置状态,不做计费探测。
        """
        checks: dict = {}

        # 内部存储:ping 一次真实往返(2s 上限)。部分测试/嵌入方不提供
        # session_store,缺省视为 ok 并标注 skipped。
        storage_ok = True
        # SessionStore.backend 是方法(非 property,TaskStore 以 backend() 复用);
        # 这里直接读内部 _backend 拿到共享 StorageBackend 实例。
        backend = getattr(getattr(app.state, "session_store", None), "_backend", None)
        if backend is None:
            checks["storage"] = {"ok": True, "skipped": "no backend"}
        else:
            try:
                cursor = await asyncio.wait_for(
                    backend.execute("SELECT 1"), timeout=_HEALTH_PING_TIMEOUT_S
                )
                await cursor.fetchone()
                checks["storage"] = {"ok": True}
            except Exception as e:
                storage_ok = False
                checks["storage"] = {"ok": False, "error": type(e).__name__}

        # 业务数据源:逐个 SELECT 1(绕结果缓存、跳过未连接的)。
        # 错误只报类型名,不回传驱动原文(避免凭据/主机信息入响应)。
        registry = getattr(app.state, "connector_registry", None)
        ds_checks: dict[str, dict] = {}
        if registry is not None:
            names = registry.list_names()

            async def _ping_one(name: str) -> tuple[str, dict]:
                try:
                    adapter = await registry.get(name)
                    if not getattr(adapter, "is_connected", False):
                        return name, {"ok": False, "error": "NotConnected"}
                    await asyncio.wait_for(
                        adapter.execute("SELECT 1"), timeout=_HEALTH_PING_TIMEOUT_S
                    )
                    return name, {"ok": True}
                except asyncio.TimeoutError:
                    return name, {"ok": False, "error": "Timeout"}
                except Exception as e:
                    return name, {"ok": False, "error": type(e).__name__}

            ds_checks = dict(await asyncio.gather(*(_ping_one(n) for n in names)))
        checks["datasources"] = ds_checks

        gateway = getattr(app.state, "llm_gateway", None)
        providers = getattr(gateway, "_providers", None)
        checks["llm"] = {
            "configured": bool(providers),
            "providers": len(providers) if providers else 0,
        }

        if not storage_ok:
            status, http_status = "unavailable", 503
        elif any(not v.get("ok") for v in ds_checks.values()):
            status, http_status = "degraded", 200
        else:
            status, http_status = "ok", 200
        return JSONResponse(
            status_code=http_status, content={"status": status, "checks": checks}
        )

    @app.get("/v1/metrics")
    async def metrics() -> Response:
        """Prometheus text exposition(单进程 serve;计数不敏感,免鉴权)。"""
        return Response(content=render_metrics(), media_type="text/plain; version=0.0.4")

    return app
