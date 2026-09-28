"""跨入口一致性:同一个人、同一份 grants、同一个数据源,结论必须一样。

对应设计文档 §5.2 / R3:四处调用点收敛成一份实现之后,**靠这条测试钉住它**。
收敛本身不能只靠「我改过了」—— 四份手抄的策略当初也是"改过了"的。

放在 ``tests/api/`` 而不是 ``tests/services/authz/``:它要一个真的 app
(``tests/api/conftest.py`` 的夹具)才跑得起来,而它验的恰恰是**装配之后**的
行为 —— 策略单测证明不了两个入口接的是同一根线。

探针选的是两条**最便宜的、且必然经过授权判定**的路径:

- API:``GET /v1/catalog/tables`` → ``deps.require_datasource``
- MCP:``kb_status`` 工具 → ``mcp.server._authorize_datasource``

两者都只问 ``Principal.allows_datasource``,不触发 LLM、不查数据。
"""

from __future__ import annotations

import asyncio

import pytest


def _mcp_components(app) -> dict:
    return {
        name: getattr(app.state, name)
        for name in ("session_manager", "connector_registry", "kb", "config", "auth")
    }


async def _invoke(server, name: str, **args):
    """直调工具底层函数(fastmcp 的 call_tool 需要 request context)。"""
    tool = await server.get_tool(name)
    result = tool.fn(**args)
    if asyncio.iscoroutine(result):
        return await result
    return result


async def test_api_and_mcp_agree_on_every_grants_case(
    api_app, api_kb, auth_service, user_client,
):
    """(grants, 数据源) 全矩阵 —— 两个入口的放行/拒绝必须逐格相同。"""
    from trove.mcp.server import build_mcp_server

    bob = await auth_service.authenticate("bob", "bobpw")
    server = build_mcp_server(
        _mcp_components(api_app), identity={"id": bob["id"], "role": "user"},
    )

    default_ds = api_app.state.connector_registry.default_name
    assert default_ds, "夹具应当注册了一个默认数据源"
    other_ds = "other_db"

    cases = [
        # (grants, 请求的数据源, 期望放行)
        ([], None, True),                       # 空 grants → 默认源
        ([], other_ds, False),                  # 空 grants → 非默认源拒绝
        ([default_ds], default_ds, True),       # allowlist 命中默认源
        ([default_ds], other_ds, False),        # allowlist 未命中
        ([other_ds], other_ds, True),           # allowlist 命中非默认源
        ([other_ds], default_ds, False),        # 在默认源上也要按 allowlist 判
    ]

    for grants, requested, should_allow in cases:
        await auth_service.set_datasources(bob["id"], grants)
        label = f"grants={grants} ds={requested!r}"

        # ── API 侧 ──
        params = {} if requested is None else {"datasource": requested}
        resp = await user_client.get("/v1/catalog/tables", params=params)
        api_denied = resp.status_code == 403

        # ── MCP 侧 ──
        # kb_status 的 datasource 是必填(无默认值),所以"省略"这一格显式传
        # 解析后的默认源 —— 授不授权与是否省略无关,`datasource or default` 的
        # 解析本身由 ask_data 覆盖(见 tests/mcp 的 test_empty_grants_only_default)。
        out = await _invoke(server, "kb_status", datasource=requested or default_ds)
        # 认「not allowed」这句具体的话:未注册/未初始化也会带回 error,但那是
        # 另一个原因 —— 探针认错了 key 就会把"查不到"读成"没权限"。
        mcp_denied = "not allowed" in (out.get("error") or "")

        assert api_denied is not should_allow, f"API 判定不符: {label}"
        assert mcp_denied is not should_allow, f"MCP 判定不符: {label}"
        assert api_denied == mcp_denied, f"两个入口不一致: {label}"


async def test_listing_agrees_with_datasource_gate(
    api_app, api_kb, auth_service, user_client,
):
    """列表页见过的源,逐个去查都必须放行 —— 列表与闸门不能两套规则。"""
    from trove.mcp.server import build_mcp_server

    bob = await auth_service.authenticate("bob", "bobpw")
    server = build_mcp_server(
        _mcp_components(api_app), identity={"id": bob["id"], "role": "user"},
    )

    await auth_service.set_datasources(bob["id"], ["test_db"])
    listed = {
        d["name"] for d in (await user_client.get("/v1/catalog/datasources")).json()["datasources"]
    }
    mcp_listed = {
        d["name"] for d in (await _invoke(server, "list_datasources"))["datasources"]
    }
    # catalog 额外要求 KB 已初始化,所以两边集合可能不等;但**共识**部分必须一致
    assert mcp_listed >= listed, (
        f"MCP 列不出 catalog 认为可见的源: {listed - mcp_listed}"
    )
    for name in listed:
        assert name == "test_db"
        resp = await user_client.get("/v1/catalog/tables", params={"datasource": name})
        assert resp.status_code != 403, f"列表说可见、闸门却拒绝: {name}"
        status = await _invoke(server, "kb_status", datasource=name)
        assert "not allowed" not in (status.get("error") or "")


# ── scope 入口(require_admin / require_scope)──────────────────────


class TestScopeEntries:
    """`require_admin` 与 `require_scope` 的判定也走 policy.scopes_allow。

    原先一处用 ``in``、一处用 ``&`` 各写了一遍 —— 单元素时等价,多元素时是
    两个语义,而两处读的是同一个 token。
    """

    async def test_admin_token_without_admin_scope_is_refused(
        self, api_app, auth_service,
    ):
        from httpx import ASGITransport, AsyncClient

        admin = await auth_service.authenticate("admin", "adminpw")
        raw, _ = await auth_service.create_token(
            admin["id"], label="query-only", scopes=["query"],
        )
        transport = ASGITransport(app=api_app)
        async with AsyncClient(
            transport=transport, base_url="http://test",
            headers={"Authorization": f"Bearer {raw}"},
        ) as c:
            resp = await c.get("/v1/admin/users")
        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"]

    async def test_token_without_scopes_keeps_working(self, client):
        """存量 token(未声明 scopes)= 不限 —— catalog 新增字段不能让它失效。"""
        resp = await client.get("/v1/catalog/datasources")
        assert resp.status_code == 200


@pytest.mark.parametrize("scopes,expected", [
    (None, 200),
    (["query"], 200),
    (["export"], 403),
])
async def test_require_scope_honours_declared_scopes(
    api_app, auth_service, scopes, expected,
):
    """``require_scope`` 的多元素语义:命中任意一个即放行。"""
    from httpx import ASGITransport, AsyncClient

    bob = await auth_service.authenticate("bob", "bobpw")
    raw, _ = await auth_service.create_token(bob["id"], label="scoped", scopes=scopes)
    transport = ASGITransport(app=api_app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": f"Bearer {raw}"},
    ) as c:
        resp = await c.post("/v1/chat", json={"question": "hi"})
    # chat 挂的是 require_scope("query") —— scope 过了才会走到别的失败(如限流/LLM)
    assert (resp.status_code != 403) is (expected == 200)
