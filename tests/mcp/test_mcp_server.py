"""Trove MCP server 测试:ask_data / list_datasources / kb_status / 多轮会话。"""

from __future__ import annotations

import asyncio

import pytest

from trove.core.config import AgentConfig
from trove.services.kb.service import KbService
from trove.storage.session_store import SessionStore


@pytest.fixture
async def mcp_components(tmp_path, sqlite_registry):
    """真实 components(可答 students 的 graph + 可查状态的 KB)。"""
    from tests.conftest import ScriptedGateway, make_test_semantic_provider

    from trove.workflow.graphs import GraphServices, build_graphs

    kb = KbService(tmp_path / "kb")
    # 用与 fixture 相同的确定性语义模型写入 kb(供 kb_status/list_datasources)
    await make_test_semantic_provider(sqlite_registry, tmp_path / "kb")

    config = AgentConfig(home=str(tmp_path / "home"), target="mock/model")
    llm = ScriptedGateway([
        "query", "```sql\nSELECT name FROM students;\n```", "OK",
        "query", "```sql\nSELECT name FROM students;\n```", "OK",
    ])
    services = GraphServices(
        llm=llm,
        connectors=sqlite_registry,
        semantic_layer=getattr(sqlite_registry, "_test_semantic_provider", None),
        kb=kb,
        config=config,
    )
    from trove.agent.session import SessionManager

    manager = SessionManager(
        config=config,
        session_store=SessionStore(home_dir=str(tmp_path / "home")),
        graphs=build_graphs(services, multi_candidate=False, query_sketch=False, agentic=False),
        llm_gateway=llm,
        kb=kb,
        connectors=sqlite_registry,
    )
    try:
        yield {
            "session_manager": manager,
            "connector_registry": sqlite_registry,
            "kb": kb,
            "config": config,
        }
    finally:
        await manager.dispose()


async def _invoke(server, name: str, **args):
    """直接调用工具底层函数(fastmcp call_tool 需要 request context,测试用 fn 直调)。

    工具函数可能 async(ask_data)或 sync(kb_status/list_datasources)。
    """
    tool = await server.get_tool(name)
    result = tool.fn(**args)
    if asyncio.iscoroutine(result):
        return await result
    return result


async def test_list_tools(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert {"ask_data", "list_datasources", "kb_status"} <= names


async def test_ask_data_answers(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(server, "ask_data",
        question="What students are in Alameda county?", datasource="test_db")
    assert isinstance(payload, dict)
    assert payload["session_id"]
    assert payload["sql"]
    assert payload["row_count"] == 5
    assert payload["no_model"] is False


async def test_ask_data_multi_turn_session(mcp_components):
    """同 session_id 复用会话:第二问能看到历史(session 保持同 id)。"""
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    r1 = await _invoke(server, "ask_data",
        question="What students are in Alameda county?", datasource="test_db")
    sid = r1["session_id"]
    r2 = await _invoke(server, "ask_data",
        question="What students are in Orange county?", datasource="test_db", session_id=sid)
    assert r2["session_id"] == sid  # 同一会话被复用
    assert r2["sql"]


async def test_ask_data_requires_question(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(server, "ask_data", question="   ")
    assert "error" in payload


async def test_list_datasources_only_initialized(mcp_components, tmp_path):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(server, "list_datasources")
    names = {d["name"] for d in payload["datasources"]}
    # test_db 有 semantics.yml → 可见;无语义模型的源不可见
    assert "test_db" in names
    assert all(d.get("has_semantics") for d in payload["datasources"])


async def test_kb_status(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    ok = await _invoke(server, "kb_status", datasource="test_db")
    assert ok["connected"] is True
    assert ok["has_semantics"] is True

    missing = await _invoke(server, "kb_status", datasource="nope")
    assert missing["connected"] is False
    assert missing["kb_initialized"] is False


async def test_kb_status_requires_datasource(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    payload = await _invoke(server, "kb_status", datasource="")
    assert "error" in payload


async def test_unknown_tool_errors(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    assert await server.get_tool("nope") is None


# ── resources(MCP 三原语:只读数据)────────────────────────────

async def _read(server, uri: str) -> str:
    """read_resource → 拼接 contents 文本(fastmcp ResourceResult)。"""
    result = await server.read_resource(uri)
    return "".join(
        c.content if isinstance(c.content, str) else str(c.content)
        for c in result.contents
    )


async def test_resources_registered(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    uris = {str(r.uri) for r in await server.list_resources()}
    templates = {
        str(t.uri_template) for t in await server.list_resource_templates()
    }
    assert "trove://datasources" in uris
    assert "trove://{datasource}/schema" in templates
    assert "trove://{datasource}/semantics" in templates


async def test_datasources_resource(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    text = await _read(server, "trove://datasources")
    assert "test_db" in text  # 有 semantics.yml 的源可见


async def test_semantics_resource_returns_model(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    text = await _read(server, "trove://test_db/semantics")
    assert "semantic_model" in text  # OSSIE 语义模型原文


async def test_schema_resource_missing_placeholder(mcp_components):
    """fixture 只写 semantics.yml → schema 返回明确占位而非报错。"""
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    text = await _read(server, "trove://test_db/schema")
    assert text == "(no such KB file)"


async def test_schema_resource_returns_notes(mcp_components, tmp_path):
    """写入 schema_notes.yml 后资源返回其内容(把元数据暴露成工具底座)。"""
    import yaml

    from trove.mcp.server import build_mcp_server

    ds_dir = mcp_components["kb"].kb_dir / "test_db"
    (ds_dir / "schema_notes.yml").write_text(
        yaml.safe_dump({"students": {"desc": "student records"}}),
        encoding="utf-8",
    )
    server = build_mcp_server(mcp_components)
    text = await _read(server, "trove://test_db/schema")
    assert "student records" in text


async def test_resource_blocks_path_traversal(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    # 单段危险名可寻址 → handler 拒绝
    for evil in ("..", "."):
        assert "(invalid datasource name)" == await _read(server, f"trove://{evil}/semantics")
    # 多段穿越 URI 根本不匹配模板(datasource 不能含 "/")→ 不可解析
    with pytest.raises(Exception):
        await _read(server, "trove://../secret/semantics")


# ── prompts(MCP 三原语:可复用模板)────────────────────────────

async def _render_prompt(server, name: str, **args) -> str:
    prompt = await server.get_prompt(name)
    assert prompt is not None, f"prompt {name!r} not registered"
    result = prompt.render(args)
    if asyncio.iscoroutine(result):
        result = await result
    if isinstance(result, str):
        return result
    return str(result)


async def test_prompts_registered(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    names = {p.name for p in await server.list_prompts()}
    assert {"datasource_guide", "ask_data"} <= names


async def test_datasource_guide_prompt(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    text = await _render_prompt(server, "datasource_guide", datasource="test_db")
    assert "trove://test_db/schema" in text
    assert "trove://test_db/semantics" in text
    assert "ask_data" in text


async def test_datasource_guide_blocks_unsafe_name(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    text = await _render_prompt(server, "datasource_guide", datasource="..")
    assert "invalid datasource name" in text


async def test_ask_data_prompt(mcp_components):
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    text = await _render_prompt(
        server, "ask_data", datasource="test_db", question="有多少学生?",
    )
    assert "test_db" in text and "有多少学生?" in text


# ── 权限:非 admin identity 按数据源 grants 过滤 ──────────────────

class _FakeAuth:
    """最小 auth 替身:grants 表 + 用户表(`store` 走同一个对象)。

    用户表可以留空 —— 那就等价于「这个人查不到」,会话层据此判定**没有主体**。
    """

    def __init__(self, grants_by_user: dict, users: dict | None = None):
        self._grants = grants_by_user
        self._users = users or {}
        self.store = self

    async def get_datasources(self, user_id):
        return list(self._grants.get(user_id, []))

    async def get_user_by_id(self, uid):
        return self._users.get(uid)


def _server_with_identity(components, auth, identity):
    from trove.mcp.server import build_mcp_server

    return build_mcp_server({**components, "auth": auth}, identity=identity)


async def test_non_admin_with_grants_can_query_granted_only(mcp_components):
    """非 admin 身份:ask_data 只能查 grants 允许的数据源。"""
    auth = _FakeAuth({1: ["test_db"]})
    server = _server_with_identity(
        mcp_components, auth, {"id": 1, "role": "user", "username": "bob"},
    )
    ok = await _invoke(server, "ask_data",
        question="What students are in Alameda county?", datasource="test_db")
    assert ok["sql"] and ok["row_count"] == 5

    blocked = await _invoke(server, "ask_data",
        question="count rows", datasource="other_db")
    assert blocked.get("error") == "datasource not allowed"


async def test_non_admin_list_scoped_by_grants(mcp_components):
    """list_datasources / datasources_resource 只列 grants 内的源。"""
    auth = _FakeAuth({1: ["test_db"]})
    server = _server_with_identity(
        mcp_components, auth, {"id": 1, "role": "user", "username": "bob"},
    )
    payload = await _invoke(server, "list_datasources")
    assert {d["name"] for d in payload["datasources"]} == {"test_db"}

    text = await _read(server, "trove://datasources")
    assert "test_db" in text


async def test_non_admin_kb_status_and_resources_gated(mcp_components):
    auth = _FakeAuth({1: ["test_db"]})
    server = _server_with_identity(
        mcp_components, auth, {"id": 1, "role": "user", "username": "bob"},
    )
    # grants 内 → 正常状态
    ok = await _invoke(server, "kb_status", datasource="test_db")
    assert ok["connected"] is True
    # grants 外 → 拒绝而非泄露状态
    denied = await _invoke(server, "kb_status", datasource="other_db")
    assert "not allowed" in denied.get("error", "")
    text = await _read(server, "trove://other_db/semantics")
    assert "datasource not allowed" in text


async def test_admin_identity_unrestricted(mcp_components):
    """admin identity:与无身份一致,全量可见可答。"""
    auth = _FakeAuth({1: []})
    server = _server_with_identity(
        mcp_components, auth, {"id": 1, "role": "admin", "username": "root"},
    )
    payload = await _invoke(server, "list_datasources")
    assert "test_db" in {d["name"] for d in payload["datasources"]}
    ok = await _invoke(server, "ask_data",
        question="What students are in Alameda county?", datasource="test_db")
    assert ok["sql"] and ok["row_count"] == 5


async def test_empty_grants_only_default_datasource(mcp_components):
    """空 grants(非 admin)→ 只放行默认数据源(与 require_datasource 同语义)。"""
    auth = _FakeAuth({1: []})
    server = _server_with_identity(
        mcp_components, auth, {"id": 1, "role": "user", "username": "bob"},
    )
    # 省略 datasource → 默认源
    ok = await _invoke(server, "ask_data",
        question="What students are in Alameda county?")
    assert ok["sql"] and ok["row_count"] == 5
    # 显式指定非默认源 → 拒绝
    blocked = await _invoke(server, "ask_data",
        question="count rows", datasource="other_db")
    assert blocked.get("error") == "datasource not allowed"


async def test_identity_without_auth_denies_everything(mcp_components):
    """有身份但缺 auth 服务 → **一律拒绝**,不是只放行默认源。

    (2026-09-29 语义变更,取代 ``test_identity_without_auth_default_only``)

    原实现把这种情况按"空 grants"处理,注释写的理由是"绝不把拿不到授权依据
    的身份当作 admin 全放行" —— **意图对,机制错**。空 grants 不是拒绝,是
    「有依据且依据为空」,它仍然放行默认源。若该用户真实 grants 是 ``{sales}``
    而默认源恰好是 ``financial``,那么一次拿不到依据就把他**提权**到了
    financial。拿不到依据只有一种正确处理:不查任何数据源
    (``policy.Principal.grants is None``)。

    生产不可达 —— identity 只能由 ``main._mcp_identity_for`` 经
    ``auth.resolve_token`` 产出,没有 auth 就没有 identity。这条守的是**构造层**
    的语义:不允许"缺依赖"被翻译成任何一个授权结论。
    """
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components, identity={"id": 1, "role": "user"})
    payload = await _invoke(server, "list_datasources")
    assert payload["datasources"] == []
    # 连默认源都不放行 —— 这正是与旧语义的分界
    omitted = await _invoke(server, "ask_data",
        question="What students are in Alameda county?")
    assert omitted.get("error") == "datasource not allowed"
    blocked = await _invoke(server, "ask_data",
        question="count rows", datasource="other_db")
    assert blocked.get("error") == "datasource not allowed"


# ── 通道鉴权:trove mcp --token 解析(fail-closed)────────────────

class _TokenAuth:
    async def resolve_token(self, raw):
        return {"id": 1, "role": "admin"} if raw == "valid" else None


async def test_mcp_identity_for():
    from trove.main import _mcp_identity_for

    # stdio 本地挂载 → 无身份(不设限)
    assert await _mcp_identity_for("stdio", "127.0.0.1", None, None) is None
    # loopback + 无 token → 允许(本地开发),但不解析身份
    assert await _mcp_identity_for("sse", "127.0.0.1", None, None) is None
    # 非 loopback 网络绑定 + 无 token → 拒绝(fail-closed)
    with pytest.raises(SystemExit):
        await _mcp_identity_for("sse", "0.0.0.0", None, None)
    # 无效 token → 拒绝
    with pytest.raises(SystemExit):
        await _mcp_identity_for("sse", "0.0.0.0", "bad", _TokenAuth())
    # 有效 token → 解析出身份(供 grants 校验)
    ident = await _mcp_identity_for("sse", "0.0.0.0", "valid", _TokenAuth())
    assert ident == {"id": 1, "role": "admin"}
    # loopback + token → 同样解析身份(可选鉴权)
    assert await _mcp_identity_for("sse", "127.0.0.1", "valid", _TokenAuth()) is not None


# ── 会话挂在谁名下:执行层判定的依据 ────────────────────────────
#
# 执行前的授权门(设计 §5.3 / G2)在 ``execute_sql`` 里读 ``state.principal``,
# 而这个主体由 ``SessionManager._principal_wire`` 从 ``session.user_id`` 现算。
# 于是 MCP 这条链路有一个容易漏的地方:MCP 自己有一个 ``identity``,但会话是
# ``start_session()`` 造的 —— 不把身份传下去,会话就挂在 ``"local"`` 这个
# 「本机可信」哨兵上,执行层那道门判的是**另一个人**。


def _wire_auth(components, auth):
    """把 auth 接到 session_manager 上 —— 生产里 main.py 就是这么接的。"""
    components["session_manager"]._auth = auth
    return auth


async def test_ask_data_session_is_opened_for_the_caller(mcp_components):
    """MCP 会话必须挂在调用者名下,不是 ``"local"``。

    今天这条差异还看不出后果(A2 已在工具边界上用真身份判过),但执行层那道
    门是新加的判定点:它读到的身份若不是调用者,任何**将来**依赖身份的执行期
    判定(表级、行级、脱敏)在 MCP 路径上都会被静默绕过,而且看不出绕过。
    """
    auth = _wire_auth(mcp_components, _FakeAuth(
        {1: ["test_db"]},
        users={1: {"id": 1, "username": "bob", "role": "user"}},
    ))
    server = _server_with_identity(
        mcp_components, auth, {"id": 1, "role": "user", "username": "bob"},
    )
    out = await _invoke(server, "ask_data",
        question="What students are in Alameda county?", datasource="test_db")

    session = await mcp_components["session_manager"].load_session(
        out["session_id"], ".",
    )
    assert session.user_id == "1", (
        f"会话挂在 {session.user_id!r} 上 —— 执行层将拿它当判定依据"
    )


async def test_ask_data_denies_when_the_identity_has_no_user_row(mcp_components):
    """身份解析不出来 → **执行层拒绝**,不是「退回本机管理员」。

    I7 的方向:拿不到依据不等于依据为空。若会话仍挂在 ``"local"`` 上,同一次
    调用会被判成本机可信身份**放行**,而调用者明明是拿 token 进来的 ——
    这条用例就是在钉这个区别。
    """
    auth = _wire_auth(mcp_components, _FakeAuth({1: ["test_db"]}, users={}))
    server = _server_with_identity(
        mcp_components, auth, {"id": 1, "role": "user", "username": "bob"},
    )
    out = await _invoke(server, "ask_data",
        question="What students are in Alameda county?", datasource="test_db")

    assert out["row_count"] != 5, "被拒的查询竟然执行了"
    assert "AUTHZ_NO_PRINCIPAL" in out.get("error", ""), (
        "拒绝原因没回到调用者 —— 空 answer + 空 verdict 读起来像「模型没答」"
    )


# ── 行动提案拉通道(补丁 4:与 webhook 推送并列的取件口)────────────
#
# 边界:这里**只读 + 回执**。approve / reject / dispatch 是人在管理台做的
# 决定,机器通道不投票 —— 资源与工具里不出现任何审批动词,这条用例组把
# 它钉在工具清单上。

_PAYLOAD_TEMPLATE = ('{"rule": "{{rule_id}}", "metric": "{{metric}}", '
                     '"current": {{current}}, "msg": "{{message}}"}')


@pytest.fixture
async def mcp_actions(mcp_components, tmp_path):
    """接上行动层(真 store/模板服务;MCP 不外送,分发器无需通道)。"""
    from types import SimpleNamespace

    from trove.services.action.dispatcher import ActionDispatcher
    from trove.services.action.service import ActionService
    from trove.services.action.store import ActionStore
    from trove.services.action.templates import ActionTemplateService

    store = ActionStore(tmp_path / "action-root")
    templates = ActionTemplateService(tmp_path / "templates")
    templates.create({
        "name": "notify-ops", "title": "Notify ops",
        "target": {"channel": "ops-alerts"}, "risk": "medium",
        "payload_template": _PAYLOAD_TEMPLATE,
    })
    templates.confirm("notify-ops")
    service = ActionService(store, templates, ActionDispatcher({}), enabled=True)
    mcp_components["actions"] = service
    env = SimpleNamespace(components=mcp_components, service=service,
                          templates=templates, store=store)
    try:
        yield env
    finally:
        # aiosqlite worker 线程常驻非 daemon,不 dispose 会挂住 pytest 退出
        await store.dispose()


async def _seed_mcp_proposal(service, *, datasource="test_db", digest="d1"):
    from trove.services.decision.rules import ActionRef, DecisionRule, Subject
    from trove.services.decision.service import DecisionOutcome

    rule = DecisionRule(
        id="revenue-drop", name="Revenue drop", severity="warning", priority=2,
        recommendation="Check the campaign calendar",
        subject=Subject(metrics=["revenue"]),
        action=ActionRef(template="notify-ops", autonomy="propose"),
    )
    outcome = DecisionOutcome(
        triggered=True, message="[warning] Revenue drop", rule_id="revenue-drop",
        severity="warning", error="",
        evidence={"rule_digest": digest,
                  "times": {"anchor_date": "2026-10-03"},
                  "rows": [{"dim": "north", "triggered": True, "current": 1234,
                            "baseline": 1400, "delta": -166,
                            "delta_pct": -0.1186, "contribution": -166.0}]},
    )
    p = await service.propose_from_verdict(
        rule=rule, outcome=outcome, datasource=datasource)
    assert p is not None
    return p


async def test_proposals_surface_registers_read_only_verbs(mcp_actions):
    """工具清单里**没有任何审批动词** —— 机器不能投票(设计边界,逐字钉)。"""
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_actions.components)
    names = {t.name for t in await server.list_tools()}
    assert {"list_proposals", "fetch_proposal", "ack_proposal"} <= names
    assert not any(
        v in n for n in names for v in ("approve", "reject", "dispatch")
    ), f"机器通道混进了审批动词: {sorted(names)}"

    uris = {str(r.uri) for r in await server.list_resources()}
    assert "trove://proposals" in uris


async def test_proposals_resource_only_approved_and_unexpired(mcp_actions):
    """拉通道索引 = **approved 且未过期** —— 它还取不走的、已经太迟的,都不发。"""
    from trove.mcp.server import build_mcp_server

    p_approved = await _seed_mcp_proposal(mcp_actions.service, digest="d1")
    await mcp_actions.service.approve(p_approved.id, "admin")
    p_pending = await _seed_mcp_proposal(mcp_actions.service, digest="d2")

    # 过去钟里建的提案:expires_at(= 时刻 + 72h TTL)落在真实现在之前
    # (但相对服务自己的钟未过期,所以能批)—— 拉通道必须把它滤掉。
    from datetime import datetime, timedelta

    past = datetime.now() - timedelta(hours=80)
    mcp_actions.service._now = staticmethod(lambda: past)
    p_stale = await _seed_mcp_proposal(mcp_actions.service, digest="d3")
    await mcp_actions.service.approve(p_stale.id, "admin")
    del mcp_actions.service._now

    server = build_mcp_server(mcp_actions.components)
    text = await _read(server, "trove://proposals")
    assert p_approved.id in text
    assert "test_db" in text and "revenue-drop" in text
    assert p_pending.id not in text, "pending 还没被批准,拉通道不该发"
    assert p_stale.id not in text, "已过期的取不走,不该出现在索引里"


async def test_fetch_proposal_returns_frozen_payload_and_trail(mcp_actions):
    from trove.mcp.server import build_mcp_server

    p = await _seed_mcp_proposal(mcp_actions.service)
    await mcp_actions.service.approve(p.id, "admin")
    server = build_mcp_server(mcp_actions.components)

    out = await _invoke(server, "fetch_proposal", proposal_id=p.id)
    detail = out["proposal"]
    assert detail["id"] == p.id and detail["status"] == "approved"
    assert "payload" not in detail, "摘要不含 payload —— 那是 fetch 的事"
    # 被批准的就是这份 payload:创建时定稿,取回时不重渲染
    assert out["payload"]["current"] == 1234
    assert out["payload"]["rule"] == "revenue-drop"
    assert [a["action"] for a in out["approvals"]] == ["approve"]
    assert out["approvals"][0]["user"] == "admin"
    assert out["deliveries"] == []
    assert out["stale"] is False


async def test_fetch_and_ack_unknown_is_no_existence_oracle(mcp_actions):
    """「存在但你没授权」与「不存在」**同一句话** —— 否则错误文案本身
    就是一个存在性预言(拿它枚举别人的提案 id)。"""
    from trove.mcp.server import build_mcp_server

    auth = _FakeAuth({1: ["test_db"]})
    p_other = await _seed_mcp_proposal(
        mcp_actions.service, datasource="other_db", digest="d9")
    await mcp_actions.service.approve(p_other.id, "admin")
    server = _server_with_identity(
        mcp_actions.components, auth,
        {"id": 1, "role": "user", "username": "bob"},
    )

    denied = await _invoke(server, "fetch_proposal", proposal_id=p_other.id)
    ghost = await _invoke(server, "fetch_proposal", proposal_id="p-ghost")
    assert denied["error"] == ghost["error"].replace("p-ghost", p_other.id), \
        "授权外的提案与不存在的提案必须不可区分"

    denied_ack = await _invoke(server, "ack_proposal", proposal_id=p_other.id)
    assert denied_ack["error"] == denied["error"]

    # 同一身份下 grants 内的提案照常可见
    p_ok = await _seed_mcp_proposal(mcp_actions.service, digest="d8")
    await mcp_actions.service.approve(p_ok.id, "admin")
    out = await _invoke(server, "fetch_proposal", proposal_id=p_ok.id)
    assert out["proposal"]["id"] == p_ok.id


async def test_ack_marks_delivered_records_actor_and_is_idempotent(mcp_actions):
    from trove.mcp.server import build_mcp_server

    p = await _seed_mcp_proposal(mcp_actions.service)
    await mcp_actions.service.approve(p.id, "admin")
    auth = _FakeAuth({1: ["test_db"]})
    server = _server_with_identity(
        mcp_actions.components, auth,
        {"id": 1, "role": "user", "username": "bob"},
    )

    out = await _invoke(server, "ack_proposal", proposal_id=p.id, note="已执行")
    assert out["acked"] is True and out["proposal"]["status"] == "delivered"

    again = await _invoke(server, "ack_proposal", proposal_id=p.id)
    assert again["acked"] is True and again["proposal"]["status"] == "delivered"

    detail = await _invoke(server, "fetch_proposal", proposal_id=p.id)
    acks = [a for a in detail["approvals"] if a["action"] == "ack"]
    assert len(acks) == 1, "重复签收不得再记一条"
    assert acks[0]["user"] == "1", "回执记调用者身份"
    assert [d["channel"] for d in detail["deliveries"]] == ["ack"]

    # pending 的批不了也签不了 —— 签收不是审批
    p2 = await _seed_mcp_proposal(mcp_actions.service, digest="d7")
    bad = await _invoke(server, "ack_proposal", proposal_id=p2.id)
    assert "pending" in bad["error"]


async def test_list_proposals_status_validation_and_filters(mcp_actions):
    from trove.mcp.server import build_mcp_server

    p = await _seed_mcp_proposal(mcp_actions.service)
    await mcp_actions.service.approve(p.id, "admin")
    server = build_mcp_server(mcp_actions.components)

    out = await _invoke(server, "list_proposals", status="approved")
    assert [x["id"] for x in out["proposals"]] == [p.id]
    assert out["enabled"] is True

    empty = await _invoke(server, "list_proposals", status="rejected")
    assert empty["proposals"] == []

    bad = await _invoke(server, "list_proposals", status="frobnicate")
    assert "unknown status" in bad["error"]

    other = await _invoke(server, "list_proposals", datasource="nope")
    assert other["proposals"] == []


async def test_missing_action_layer_degrades_cleanly(mcp_components):
    """没装行动层的进程:工具与资源各回一句明确的话,不是异常。"""
    from trove.mcp.server import build_mcp_server

    server = build_mcp_server(mcp_components)
    listed = await _invoke(server, "list_proposals")
    assert "not available" in listed.get("error", ""), listed
    for name in ("fetch_proposal", "ack_proposal"):
        out = await _invoke(server, name, proposal_id="p-x")
        assert "not available" in out.get("error", ""), (name, out)
    assert "not available" in await _read(server, "trove://proposals")
