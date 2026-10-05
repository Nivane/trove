"""W5 阶段二:analyst 只读面 —— 冻结清单的守卫回归门。

设计稿 §2.2 R1/R3:analyst 只读面是**逐条挑出的只读 GET**,不是一把梭。
本文件钉住三件事:

1. **守卫清单**:app 里 analyst 可过的 ``/v1/admin`` 路由集合,必须与
   冻结清单逐条相等 —— 以后谁把写端点换成 ``require_admin_or_analyst``、
   或顺手多开一条 GET,这里都会红。清单本身就是文档:StringLiteral 没有的
   别处可以再核。
2. **HTTP 行为**:analyst 在冻结读面 200、写端点与未开 GET 403;``user``
   角色在开了的读面上仍然 403(角色集只放宽到 analyst,不放宽到 user)。
3. **受限 token**:声明了 scopes 的 analyst token 缺 ``admin`` scope 仍
   403 —— 与 ``require_admin`` 同一套最小权限语义,不因角色放宽而放松。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

#: analyst 可过的 /v1/admin 路由冻结清单(W5 阶段二第一批)。
#: 规格 §4.6 的 8 条(overview / quality / usage / todos / coverage / audit /
#: sessions / users)之外,扩的都是**可见页端到端点得开**所必需的只读依赖:
#: jobs ×3(运营组),checkpoint 钻取 ×2(检查点页),drift ×3 + datasources
#: 列表 + semantic 详情/history(治理中心),decisions 列表(任务页读规则名)。
#: **全部 GET,写端点零开放** —— 见 test_write_endpoints_never_enter_analyst_surface。
ANALYST_READ_SURFACE = {
    ("GET", "/v1/admin/overview"),
    ("GET", "/v1/admin/quality/overview"),
    ("GET", "/v1/admin/usage/overview"),
    ("GET", "/v1/admin/todos"),
    ("GET", "/v1/admin/coverage"),
    ("GET", "/v1/admin/audit"),
    # 拒绝频率报表 = ``/v1/admin/audit`` 同表(同守卫)的只读投影:审计页
    # 卡片端到端点得开;不新增数据面(读的还是 analyst 本就能读的审计行)。
    ("GET", "/v1/admin/audit/refusal-report"),
    ("GET", "/v1/admin/sessions"),
    ("GET", "/v1/admin/sessions/{session_id}/checkpoints"),
    ("GET", "/v1/admin/sessions/{session_id}/checkpoints/{checkpoint_id}"),
    ("GET", "/v1/admin/users"),
    ("GET", "/v1/admin/datasources"),
    ("GET", "/v1/admin/jobs"),
    ("GET", "/v1/admin/jobs/{job_id}"),
    ("GET", "/v1/admin/jobs/{job_id}/runs"),
    ("GET", "/v1/admin/decisions"),
    ("GET", "/v1/admin/drift"),
    ("GET", "/v1/admin/drift/runs"),
    ("GET", "/v1/admin/drift/{drift_id}"),
    ("GET", "/v1/admin/semantic/{name}"),
    ("GET", "/v1/admin/semantic/{name}/history"),
}

#: 无参即可调用的读面(HTTP 直测 200;带路径/必填参数的另测「非 403」)。
_READS_NO_PARAMS = [
    "/v1/admin/overview",
    "/v1/admin/quality/overview",
    "/v1/admin/usage/overview",
    "/v1/admin/todos",
    "/v1/admin/coverage",
    "/v1/admin/audit",
    "/v1/admin/sessions",
    "/v1/admin/users",
    "/v1/admin/datasources",
]


@pytest.fixture
async def analyst_token(auth_service):
    """Role=analyst 用户 + 不限 scope 的 Bearer token。"""
    await auth_service.create_user("ana", "anapw", role="analyst")
    user = await auth_service.authenticate("ana", "anapw")
    raw, _ = await auth_service.create_token(user["id"], label="test-analyst")
    return raw


@pytest.fixture
async def analyst_client(api_app, analyst_token):
    """Authenticated analyst client."""
    transport = ASGITransport(app=api_app)
    headers = {"Authorization": f"Bearer {analyst_token}"}
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=headers
    ) as c:
        yield c


def _route_guards(route) -> set:
    """路由**直接**依赖里的守卫函数集合(FastAPI 依赖树的一层)。

    ``_EffectiveRouteContext`` 与 APIRoute 都暴露 ``dependant``。
    """
    dep = getattr(route, "dependant", None)
    return {d.call for d in dep.dependencies} if dep else set()


def _iter_effective_routes(app):
    """枚举装配后的有效路由。

    FastAPI ≥0.141 的 ``include_router`` 是惰性 ``_IncludedRouter`` 包装,
    扁平遍历 ``app.routes`` 拿不到真正的 APIRoute —— 要展开
    ``effective_candidates()``。旧版 FastAPI 直接就是扁平列表,原样回退。
    """
    try:
        from fastapi.routing import _IncludedRouter
    except ImportError:  # pragma: no cover - 旧版 FastAPI 回退
        yield from app.routes
        return
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        if isinstance(route, _IncludedRouter):
            stack.extend(route.effective_candidates())
        else:
            yield route


def _admin_routes(app) -> list:
    return [
        r
        for r in _iter_effective_routes(app)
        if getattr(r, "path", "").startswith("/v1/admin")
    ]


async def test_analyst_surface_is_the_frozen_get_list(api_app):
    """守卫清单回归门:analyst 可过的路由集合 == 冻结清单,逐条相等。"""
    from trove.api.deps import require_admin, require_admin_or_analyst

    analyst_open: set[tuple[str, str]] = set()
    unguarded: set[tuple[str, str]] = set()
    for route in _admin_routes(api_app):
        guards = _route_guards(route)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            if require_admin_or_analyst in guards:
                analyst_open.add((method, route.path))
            elif require_admin not in guards:
                unguarded.add((method, route.path))

    assert not unguarded, (
        f"/v1/admin 路由缺 admin 守卫(既不是 require_admin 也不是 "
        f"require_admin_or_analyst): {sorted(unguarded)}"
    )
    assert analyst_open == ANALYST_READ_SURFACE, (
        f"analyst 可过的路由与冻结清单不一致。\n"
        f"多开: {sorted(analyst_open - ANALYST_READ_SURFACE)}\n"
        f"缺失: {sorted(ANALYST_READ_SURFACE - analyst_open)}"
    )


async def test_write_endpoints_never_enter_analyst_surface(api_app):
    """GET-only 纪律:冻结清单里任何一条都不是写方法(防以后手滑)。"""
    from trove.api.deps import require_admin_or_analyst

    for route in _admin_routes(api_app):
        if require_admin_or_analyst in _route_guards(route):
            assert "GET" in route.methods and route.methods - {"GET", "HEAD", "OPTIONS"} == set(), (
                f"{route.path} 开了 analyst 但不是纯 GET: {sorted(route.methods)}"
            )


async def test_analyst_reads_open_surface(analyst_client, api_kb):
    """analyst 在冻结读面无参端点全部 200(api_kb 给 todos/coverage 源)。"""
    for path in _READS_NO_PARAMS:
        resp = await analyst_client.get(path)
        assert resp.status_code == 200, (path, resp.status_code, resp.text[:200])


async def test_analyst_reads_param_surface_not_forbidden(analyst_client, api_kb):
    """带参数读面:守卫放行即可(404/400 是夹具没数据,不是 403)。"""
    cases = [
        "/v1/admin/decisions?datasource=test_db",
        "/v1/admin/semantic/test_db",
        "/v1/admin/semantic/test_db/history",
        # drift 系列要求显式 datasource(400 属参数校验,守卫已过)。
        "/v1/admin/drift?datasource=test_db",
        "/v1/admin/drift/runs?datasource=test_db",
        "/v1/admin/drift/1?datasource=test_db",
        # api_app 夹具未装配 JobsService(409 "jobs not configured")——
        # 守卫已过(409≠403);200 路径由 jobs 服务自己的测试覆盖。
        "/v1/admin/jobs",
    ]
    for path in cases:
        resp = await analyst_client.get(path)
        assert resp.status_code != 403, (path, resp.status_code, resp.text[:200])


async def test_analyst_writes_stay_denied(analyst_client, api_app):
    """写端点(含 analyst 可见页里的写动作)对 analyst 一律 403。"""
    cases = [
        ("DELETE", "/v1/admin/users/999999", None),
        ("POST", "/v1/admin/drift/check", None),
        ("POST", "/v1/admin/drift/1/resolve", {"reason": "x"}),
        ("POST", "/v1/admin/sessions/s1/checkpoints/c1/resume", {"workflow": "reflection"}),
        ("DELETE", "/v1/admin/jobs/whatever", None),
    ]
    for method, path, body in cases:
        resp = await analyst_client.request(method, path, json=body)
        assert resp.status_code == 403, (method, path, resp.status_code, resp.text[:200])


async def test_analyst_admin_only_reads_stay_denied(analyst_client):
    """未开的 GET(用户页细读 / 系统组 / 建模组 / 决策细读)对 analyst 403。"""
    cases = [
        "/v1/admin/settings",
        "/v1/admin/skills",
        "/v1/admin/memory/profile",
        "/v1/admin/users/1/tokens",
        "/v1/admin/decisions/raw?datasource=test_db",
        "/v1/admin/datasources/test_db",
    ]
    for path in cases:
        resp = await analyst_client.get(path)
        assert resp.status_code == 403, (path, resp.status_code, resp.text[:200])


async def test_user_role_stays_denied_on_open_surface(user_client):
    """角色集只放宽到 analyst:user 在同一个开了的读面上仍 403。"""
    resp = await user_client.get("/v1/admin/overview")
    assert resp.status_code == 403, (resp.status_code, resp.text[:200])


async def test_restricted_analyst_token_needs_admin_scope(api_app, auth_service):
    """受限 token 缺 admin scope → 403;角色是 analyst 不豁免最小权限。"""
    await auth_service.create_user("ana2", "anapw", role="analyst")
    user = await auth_service.authenticate("ana2", "anapw")
    raw, _ = await auth_service.create_token(
        user["id"], label="restricted", scopes=["query"]
    )
    transport = ASGITransport(app=api_app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {raw}"},
    ) as c:
        resp = await c.get("/v1/admin/overview")
        assert resp.status_code == 403, (resp.status_code, resp.text[:200])
