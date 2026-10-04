"""Auth dependencies for API routes.

FastAPI dependency-based auth (not global middleware) keeps
Every protected endpoint adds ``user: dict = Depends(get_current_user)``
(or ``Depends(require_admin)``); the resolved user is also attached to
``request.state.user`` for audit.

``NullAuth`` is the fallback when a components dict lacks an ``auth``
service (embedded/stray ``create_app`` callers): every request runs as a
synthetic local admin and a loud warning is logged once. ``trove serve``
always injects a real AuthService, so this only exists to keep tests and
embedding code from hard-breaking.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, Header, HTTPException, Request

from trove.services.authz.policy import Policy, Principal, scopes_allow

logger = logging.getLogger(__name__)

LOCAL_ADMIN = {
    "id": "local",
    "username": "local",
    "role": "admin",
    "display_name": "Local (auth disabled)",
    "disabled": False,
}


class NullAuth:
    """Stand-in auth service when no AuthService was injected."""

    async def resolve_token(self, raw_token: str) -> dict[str, Any] | None:
        return dict(LOCAL_ADMIN)

    async def get_datasources(self, user_id: int) -> list[str]:
        return []

    async def get_topic_grants(self, user_id: int) -> dict[str, list[str]] | None:
        # NullAuth 的请求一律解析成 LOCAL_ADMIN(admin 判定不读域级授权);
        # 这个实现只为 duck-type 完整 —— 没有存储 = 没有收窄配置。
        return None

    async def record_audit(self, *args, **kwargs) -> None:
        pass


def _get_auth(request: Request) -> Any:
    auth = getattr(request.app.state, "auth", None)
    if auth is None:
        auth = NullAuth()
        request.app.state.auth = auth
        logger.warning(
            "AUTH DISABLED — no auth service in components; "
            "requests run as synthetic local admin"
        )
    return auth


def _policy(request: Request) -> Policy:
    """本 app 的策略实例(单例)。

    判定规则本身在 ``services/authz/policy.py``;这里只是拿到那份实现,
    不在这层再写一次判定 —— 见该模块 docstring 的「四份副本」。
    """
    policy = getattr(request.app.state, "policy", None)
    if policy is None:
        policy = Policy(_get_auth(request))
        request.app.state.policy = policy
    return policy


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Resolve the Bearer token to a user dict (401 on missing/invalid)."""
    auth = _get_auth(request)
    if isinstance(auth, NullAuth):
        request.state.user = dict(LOCAL_ADMIN)
        return request.state.user

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raw = authorization[len("Bearer "):].strip()
    user = await auth.resolve_token(raw)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="missing or invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.user = user
    return user


async def get_principal(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> Principal:
    """本请求的授权主体(带 grants),按需构造并缓存在 ``request.state``。

    惰性是有意的:构造要读一次 grants 表,而多数端点(管理台、KB、facts)
    根本不判数据源。每次请求都预先读一遍是白付的。

    缺失时构造而非报错 —— 直调 ``require_datasource`` 的调用方(路由里已有
    ``Depends(get_current_user)``)不该被迫再走一遍依赖注入。
    """
    principal = getattr(request.state, "principal", None)
    if principal is None:
        principal = await _policy(request).principal_for(user)
        request.state.principal = principal
    return principal


async def require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Admin-only guard (403 for non-admin roles).

    同时执行 token 级最小权限:受限 token(声明了 scopes)必须在 scopes
    里带 ``admin`` 才能访问管理端点;未声明 scopes 的 token(= 不限)沿用
    角色裁决。这样为 admin 账号签发的 query 专用 token 不会意外获得管理权。
    """
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="admin privileges required")
    if not scopes_allow(user.get("scopes"), "admin"):
        raise HTTPException(
            status_code=403,
            detail="token lacks the 'admin' scope (restricted token)",
        )
    return user


async def require_admin_or_analyst(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """admin/analyst 守卫(W5 阶段二 analyst 只读面)。

    与 ``require_admin`` 同一套 token 最小权限语义,只把角色集从 {admin}
    放宽到 {admin, analyst},**且只挂在逐条挑出的只读 GET 路由上** ——
    写端点、用户与权限页、系统组一律保持 ``require_admin``。一把梭会把
    几十处写端点也开给 analyst,这正是「逐条切换」纪律要防的事。

    角色可见性三处一致(设计稿 §2.2 R1):navModel.ts 的 ``visibleFor``、
    路由 ``meta.roles`` 与本守卫 —— 一个导航项只有端点对该角色 200 才
    允许显示;反方向不要求(数据依赖端点可先开,页面仍可隐藏)。
    """
    if user["role"] not in ("admin", "analyst"):
        raise HTTPException(
            status_code=403, detail="admin or analyst privileges required"
        )
    if not scopes_allow(user.get("scopes"), "admin"):
        raise HTTPException(
            status_code=403,
            detail="token lacks the 'admin' scope (restricted token)",
        )
    return user


def require_scope(*required: str):
    """Route-level token scope gate (dependency factory).

    语义:token 未声明 scopes(存量/不限) → 放行;声明了 scopes 的受限
    token → 必须命中至少一个 required scope,否则 403。配合
    ``require_admin`` 的 admin scope,``["query"]`` 的 token 可查不可管。
    """

    async def _check(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        if not scopes_allow(user.get("scopes"), *required):
            raise HTTPException(
                status_code=403,
                detail=f"token lacks required scope(s): {', '.join(sorted(required))}",
            )
        return user

    return _check


async def require_datasource(
    request: Request,
    datasource: str | None,
    user: dict[str, Any] = Depends(get_current_user),
) -> str:
    """Resolve and authorize a datasource.

    Admin: any datasource (explicit or the registry default).
    User: grants from the auth service — empty grants allow only the
    registry default (single-datasource deployments need no grant setup);
    non-empty grants are a strict allowlist.

    ``grants`` 为 None(拿不到授权依据)时**拒绝**,不放行默认源 —— 三种取值
    的语义见 :class:`~trove.services.authz.policy.Principal`。

    判定本身在策略层(``services/authz/policy.py``)。这里只做三件事:解析目标
    数据源、把主体挂到 ``request.state.principal``、问策略要一个结论。

    主体挂在 request 上是为了 P3 —— ``Authorizer`` 要把它从请求边界带进
    ``WorkflowState`` 才能在执行 SQL 前再验一次(设计 §5.1)。**P1 还没有接这段
    线**:图内节点目前拿不到 principal,这是 P3 的活。

    Returns the resolved datasource name (the router may pass the same
    value through its own resolution).
    """
    registry = getattr(request.app.state, "connector_registry", None)
    default_name = registry.default_name if registry is not None else None
    target = datasource or default_name
    if not target:
        raise HTTPException(status_code=400, detail="no active datasource")

    principal = await get_principal(request, user)
    if not principal.allows_datasource(target, default_name):
        raise HTTPException(status_code=403, detail=f"datasource not allowed: {target}")
    return target


async def check_api_rate(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> None:
    """业务端点限流(按 user 的进程内令牌桶 + 日历日配额)。

    配置读 ``app.state.config``(admin 设置热更新即时生效);0 = 关闭。
    限流命中 → 429 + Retry-After。先过每分钟桶,再计日配额(被限流的
    请求不计入当日配额)。
    """
    limiter = getattr(request.app.state, "rate_limiter", None)
    config = getattr(request.app.state, "config", None)
    if limiter is None:
        return
    rpm = getattr(config, "api_rate_per_minute", 0) if config is not None else 0
    daily = getattr(config, "api_daily_quota", 0) if config is not None else 0
    if rpm <= 0 and daily <= 0:
        return
    key = f"user:{user['id']}"
    if rpm > 0:
        allowed, retry_after = limiter.allow(key, rpm)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="too many requests — slow down",
                headers={"Retry-After": str(retry_after)},
            )
    if daily > 0:
        allowed_daily, _reset = limiter.allow_daily(key, daily)
        if not allowed_daily:
            raise HTTPException(
                status_code=429,
                detail="daily request quota exceeded — try again tomorrow",
            )
