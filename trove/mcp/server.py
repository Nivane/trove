"""Trove MCP server — 把 NL→SQL 问答能力暴露为 MCP tools + resources。

用官方 ``fastmcp`` 高层封装。工具面:

- ``ask_data``:自然语言提问 → 答案/SQL/行数/verdict/拒绝信息。多轮
  会话用 ``session_id`` 参数复用(进程内会话注册表;缺失则新建)。
- ``list_datasources``:已连接且 KB 已初始化的数据源(用户端可见性规则)。
- ``kb_status``:数据源连接 / KB 初始化 / 语义模型文件状态。
- ``list_proposals`` / ``fetch_proposal`` / ``ack_proposal``:行动提案的
  **拉通道**(P3,与 webhook 推送并列的第二条出口)。拉取已批准的提案、
  取回 payload、回执 —— **审批本身不在这里**:approve / reject / dispatch
  是管理台里的人做的决定,agent 只能取和签收。

资源面(MCP 三原语之一:只读数据):

- ``trove://datasources``:可答数据源清单。
- ``trove://{datasource}/schema``:schema_notes.yml 原文(表/字段/口径注释)。
- ``trove://{datasource}/semantics``:semantics.yml 原文(OSSIE 语义模型)。
  ——把元数据暴露成 MCP server,数据平台成为 agent 的工具底座(只读、
  无副作用)。
- ``trove://proposals``:已批准且未过期的行动提案索引(拉通道的入口;
  详情走 ``fetch_proposal``)。

模板面(MCP 三原语之三:可复用提示词):

- ``datasource_guide``:查数据源的规范工作流(先读 schema/semantics 资源
  再 ask_data)。
- ``ask_data``:向数据源提问的模板。

语义优先(Phase B)天然生效:无语义模型的数据源 ask_data 会明确拒绝并
提示 /kb init;未覆盖查询 → 拒绝 + draft。``trove mcp`` 命令以 stdio
transport 启动(供 Claude Code / 其他 MCP 客户端本地挂载)。

**权限边界**:MCP 通道默认不带用户身份——stdio 本地挂载视作本机可信
(等价 admin);HTTP transport 由 ``main.py`` 把 ``--token`` 解析为真实
用户 token 后传入 ``identity``,本模块据此做数据源 grant 校验。identity
缺失时保持旧行为(不设限)。

grant 判定的实现**不在本模块** —— 唯一实现在
``services/authz/policy.py``,与 ``api/deps.require_datasource`` 共用同一份。
本模块只负责把 ``identity`` 交给策略、拿回一个 ``Principal``。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastmcp import FastMCP

from trove.services.action.models import PROPOSAL_STATUSES
from trove.services.authz.policy import (
    LOCAL_SUBJECT,
    Policy,
    Principal,
    visible_datasources,
)

logger = logging.getLogger(__name__)

_SESSION_CACHE_MAX = 200

#: 拉通道一次最多带回的提案数(内务:提案对账是人过的,不是 agent 全量扫)。
_PROPOSAL_LIMIT_MAX = 100
#: 拉通道的默认取件状态:已批准 = 等人取走的那些。
_PULL_STATUS = "approved"


def build_mcp_server(
    components: dict, identity: dict[str, Any] | None = None,
) -> FastMCP:
    """components(create_app_components 产物)→ 已注册工具的 FastMCP server。

    ``identity`` — 调用者用户 dict(``{"id", "role", ...}``);None/role=admin
    → 不设限(本地 stdio 等价 admin);非 admin → 数据源按 grants 过滤。
    """
    session_manager = components["session_manager"]
    connector_registry = components["connector_registry"]
    kb = components["kb"]
    config = components["config"]

    mcp = FastMCP("trove")

    if identity is not None and components.get("auth") is None:
        # 有身份却拿不到 grants 表 → 该身份会被判「无授权依据」而一律拒绝,
        # 表现为 list_datasources 返回空。空列表读起来像「一个数据源都没有」,
        # 与真实原因差得很远,所以这里必须出声(不静默降级)。
        # 生产不可达:identity 由 main._mcp_identity_for 通过 auth.resolve_token
        # 产出,没有 auth 就没有 identity。
        logger.warning(
            "MCP got an identity but no auth component — every datasource "
            "will be denied for user %r (no way to resolve grants)",
            identity.get("id"),
        )

    # 进程内会话注册表:session_id → Session(ask_data 多轮复用)
    sessions: dict[str, Any] = {}

    async def _get_session(session_id: str | None) -> tuple[str, Any]:
        """返回 (effective_session_id, Session)。传入的 id 已注册 → 复用。

        **会话挂在调用者名下**(``identity["id"]``),不是默认的 ``"local"``:
        执行前的授权门在 ``execute_sql`` 里读 ``state.principal``,而主体正是
        会话层从 ``session.user_id`` 现算的。挂错人,那道门判的就是另一个人 ——
        今天 A2 已经在工具边界上用真身份判过、看不出差别,但任何**将来**依赖
        身份的执行期判定(表级 / 行级 / 脱敏)会被静默绕过,且看不出绕过。

        ``identity=None``(stdio 本地挂载)落到 ``LOCAL_SUBJECT`` —— 与
        :meth:`Policy.local_admin` 同口径,即本机可信身份。
        """
        if session_id and session_id in sessions:
            return session_id, sessions[session_id]
        session = await session_manager.start_session(
            user_id=str(identity["id"]) if identity else LOCAL_SUBJECT,
        )
        sid = session_id or session.session_id
        sessions[sid] = session
        # 容量保护:超出后丢弃最旧(会话在 SessionStore 仍可 load_session 找回)
        while len(sessions) > _SESSION_CACHE_MAX:
            sessions.pop(next(iter(sessions)), None)
        return sid, session

    # ── 数据源授权 ────────────────────────────────────────────────
    # 判定**不在这里**:唯一实现在 services/authz/policy.py。本模块原先那套
    # `(是否受限, 允许集)` 与 api/deps.require_datasource 是同一策略的两份手抄,
    # 且已经漂移 —— 见 policy.py 模块 docstring。
    #
    # 每次判定都重新解析主体 —— **不要缓存**。
    #
    # identity(token 解析出来的用户)进程内确实不变,但主体里带的 grants 是**存
    # 在库里的可变状态**:管理台撤掉一个数据源授权,缓存住的主体要等到进程重启
    # 才生效,等于撤销授权不生效。代价是每次判定多一次 grants 读 —— 与转调前的
    # `_granted()` 同量级,不是新开销。
    policy = Policy(components.get("auth"))

    async def _principal() -> Principal:
        if identity is None:
            return Policy.local_admin()
        return await policy.principal_for(identity)

    async def _authorize_datasource(datasource: str | None) -> str | None:
        """解析并授权目标数据源;不允许 → None(调用方给友好拒绝)。"""
        target = (datasource or "").strip() or connector_registry.default_name
        if not target:
            return None
        principal = await _principal()
        allowed = principal.allows_datasource(
            target, connector_registry.default_name,
        )
        return target if allowed else None

    async def _datasources_visible() -> list[dict[str, Any]]:
        """已注册且有语义模型且当前身份可见的数据源。"""
        principal = await _principal()
        infos = connector_registry.list_info()
        allowed = set(visible_datasources(
            principal,
            (str(i.get("name") or "") for i in infos),
            connector_registry.default_name,
        ))
        out: list[dict[str, Any]] = []
        for info in infos:
            name = str(info.get("name") or "")
            if not name or name not in allowed:
                continue
            try:
                has_semantics = kb.semantics_path(name).exists()
            except Exception:
                has_semantics = False
            if has_semantics:
                out.append({"name": name, "has_semantics": True})
        return out

    def _kb_status(datasource: str) -> dict[str, Any]:
        """单个数据源的连接/KB/语义模型状态。"""
        connected = False
        try:
            connected = connector_registry.is_registered(datasource)
        except Exception:
            connected = False
        initialized = False
        semantics_exists = False
        if connected:
            try:
                initialized = kb.kb_initialized(datasource)
                semantics_exists = kb.semantics_path(datasource).exists()
            except Exception:
                pass
        return {
            "datasource": datasource,
            "connected": connected,
            "kb_initialized": initialized,
            "has_semantics": semantics_exists,
            "lang": config.language if config else "en",
        }

    def _ds_name_safe(datasource: str) -> bool:
        """数据源名须是单个路径段标识,防路径穿越(构造 kb 文件路径用)。"""
        return (
            bool(datasource)
            and "/" not in datasource
            and "\\" not in datasource
            and datasource not in (".", "..")
        )

    def _read_kb_file(path: Any) -> str:
        """只读 KB YAML 原文;缺失返回明确占位(资源语义 = 数据,不抛错)。"""
        try:
            if not path.exists():
                return "(no such KB file)"
            return path.read_text(encoding="utf-8")
        except Exception as e:
            return f"(unreadable KB file: {e})"

    # ── resources(MCP 三原语之一:只读数据——schema/语义模型)─────────────
    # 对应"MCP × 数仓工具"结合点:把元数据暴露成 MCP server,成为 agent
    # 的工具底座;resources 只读、无副作用,客户端可静态拉取比对口径。

    @mcp.resource("trove://datasources")
    async def datasources_resource() -> str:
        """Connected & KB-initialized datasources (read-only inventory)."""
        lines = [
            f"- {d['name']} (has_semantics={d.get('has_semantics', False)})"
            for d in await _datasources_visible()
        ]
        return "\n".join(lines) if lines else "(no answerable datasources)"

    async def _resource_for(datasource: str, which: str) -> str:
        ds = (datasource or "").strip()
        if not _ds_name_safe(ds):
            return "(invalid datasource name)"
        if await _authorize_datasource(ds) is None:
            return f"(datasource not allowed: {ds})"
        path = (
            kb.schema_notes_path(ds) if which == "schema" else kb.semantics_path(ds)
        )
        return _read_kb_file(path)

    @mcp.resource("trove://{datasource}/schema")
    async def schema_resource(datasource: str) -> str:
        """schema_notes.yml content for the datasource — table/column/metric
        annotation (read-only data, no side effects)."""
        return await _resource_for(datasource, "schema")

    @mcp.resource("trove://{datasource}/semantics")
    async def semantics_resource(datasource: str) -> str:
        """semantics.yml content for the datasource — the OSSIE semantic model
        (datasets + metrics, the single answerability boundary) as read-only data."""
        return await _resource_for(datasource, "semantics")

    # ── 行动提案拉通道(P3:与 webhook 推送并列的取件口)─────────────
    # 这里**只读 + 回执**:清单/详情/签收。approve / reject / dispatch 是人在
    # 管理台做的决定(MCP 是机器通道,机器不投票)—— 所以没有任何工具能改
    # 提案的审批状态,唯一的写是 ack(签收,事实记录)。
    actions = components.get("actions")

    def _actor() -> str:
        return str(identity["id"]) if identity else LOCAL_SUBJECT

    async def _allowed(p: Any) -> bool:
        """提案的数据源对当前身份可见 —— 与 ask_data 走同一份策略判定。"""
        return await _authorize_datasource(getattr(p, "datasource", "") or "") is not None

    def _proposal_brief(p: Any) -> dict[str, Any]:
        """列表/回执用的提案摘要(不含 payload —— 那是 fetch 的事)。"""
        return {
            "id": p.id,
            "datasource": p.datasource,
            "rule_id": p.rule_id,
            "template": p.template,
            "status": p.status,
            "risk": p.risk,
            "severity": p.severity,
            "priority": p.priority,
            "action_type": p.action_type,
            "rationale": p.rationale,
            "created_at": p.created_at,
            "expires_at": p.expires_at,
            "attempts": p.attempts,
            "error": p.error,
        }

    async def _missing(proposal_id: str) -> dict[str, Any]:
        """取不到与没授权**同一句话** —— 别把「存在但你看不到」漏成存在性预言。"""
        return {
            "error": f"proposal not found: {proposal_id} "
                     "(or its datasource is not allowed for this identity)"
        }

    async def _open_proposals(status: str, now_iso: str) -> list[Any]:
        """某状态且未过期的提案,按身份过滤(拉通道只发「还能取的」)。"""
        rows = await actions.list_proposals(status=status, limit=_PROPOSAL_LIMIT_MAX)
        out = []
        for p in rows:
            if p.expires_at and p.expires_at < now_iso:
                continue
            if not await _allowed(p):
                continue
            out.append(p)
        return out

    @mcp.resource("trove://proposals")
    async def proposals_resource() -> str:
        """Approved & unexpired action proposals (read-only index — fetch one
        with ``fetch_proposal`` to get its payload, then ``ack_proposal``)."""
        if actions is None:
            return "(action layer is not available in this process)"
        now_iso = datetime.now().isoformat(timespec="seconds")
        rows = await _open_proposals(_PULL_STATUS, now_iso)
        if not rows:
            return "(no approved proposals waiting)"
        lines = [
            f"- [{p.id}] datasource={p.datasource} rule={p.rule_id} "
            f"template={p.template} risk={p.risk} expires_at={p.expires_at or '-'}"
            for p in rows
        ]
        return "\n".join(lines)

    # ── prompts(MCP 三原语之三:可复用模板——标准化的提示词工作流)─────────
    # 客户端可直接拉起这些模板,把"查数据源"的规范流程固化成提示词,
    # 而不是每次手写。

    @mcp.prompt("datasource_guide")
    def datasource_guide_prompt(datasource: str) -> str:
        """How to work with a datasource: read its metadata resources first,
        then ask via ask_data (reusable workflow prompt)."""
        ds = (datasource or "").strip()
        if not _ds_name_safe(ds):
            return "(invalid datasource name)"
        return (
            f"To answer questions about datasource '{ds}':\n"
            f"1. Read trove://{ds}/schema and trove://{ds}/semantics "
            f"(table/column/metric annotations and the semantic model).\n"
            f"2. Check kb_status({ds}) for connection/KB state.\n"
            f"3. Ask natural-language questions with ask_data."
        )

    @mcp.prompt("ask_data")
    def ask_data_prompt(datasource: str, question: str) -> str:
        """Template for phrasing a question to a datasource via ask_data."""
        return (
            f"Ask the {'datasource ' + datasource if datasource else 'default datasource'}:\n"
            f"{question}"
        )

    # ── tools ─────────────────────────────────────────────

    @mcp.tool()
    async def ask_data(
        question: str,
        datasource: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Ask the datasource a natural-language question and get the answer.

        Returns Markdown answer, generated SQL, row count, verdict, and
        refusal info (semantic-model-first: uncovered questions are refused
        with a reason, never guessed). Pass an existing ``session_id`` for
        multi-turn context; omit for a fresh session.
        """
        question = (question or "").strip()
        if not question:
            return {"error": "question is required"}
        sid, session = await _get_session(session_id)
        target = await _authorize_datasource(datasource)
        if target is None:
            requested = (datasource or "").strip() or connector_registry.default_name
            return {
                "session_id": sid,
                "error": "datasource not allowed",
                "refusal": (
                    f"datasource not allowed for the MCP identity: {requested}"
                ),
            }
        try:
            state = await session_manager.ask(
                session=session, question=question, datasource=target,
                # 凭证上的 scopes 随请求走(identity=None 时无凭证层,不传)
                scopes=(identity or {}).get("scopes"),
            )
        except Exception as e:
            return {"session_id": sid, "error": f"ask failed: {e}"}
        return {
            "session_id": sid,
            "answer": state.final_response,
            "sql": state.sql,
            "row_count": state.row_count,
            "verdict": state.verdict,
            "datasource": state.datasource,
            "no_model": state.no_model,
            "refusal": state.refusal,
            # 执行期错误(含 [ERR:AUTHZ_*] 拒绝)**必须带回去**:丢掉它,
            # 被拒的调用就是「空 answer + 空 verdict」,读起来像模型没答。
            "error": state.error,
        }

    @mcp.tool()
    async def list_datasources() -> dict[str, Any]:
        """List datasources that are connected AND have a semantic model —
        the ones answerable via ask_data (semantic-first: no model = not answerable).
        Scoped to the caller's grants for a non-admin MCP identity."""
        return {"datasources": await _datasources_visible()}

    @mcp.tool()
    async def kb_status(datasource: str) -> dict[str, Any]:
        """Return connection / KB-init / semantic-model status for one datasource —
        use to decide whether to initialize the KB (kb init) first."""
        if not (datasource or "").strip():
            return {"error": "datasource is required"}
        ds = datasource.strip()
        if await _authorize_datasource(ds) is None:
            return {
                "datasource": ds,
                "error": f"datasource not allowed: {ds}",
            }
        return _kb_status(ds)

    @mcp.tool()
    async def list_proposals(
        status: str | None = None, datasource: str | None = None,
    ) -> dict[str, Any]:
        """List action proposals (a rule fired and asked a human to act).

        Approved proposals are the pull channel: fetch one with
        ``fetch_proposal`` and receipt it with ``ack_proposal``. Approving,
        rejecting and dispatching are admin-console decisions and are NOT
        available here. Empty ``status`` lists every state.
        """
        if actions is None:
            return {"error": "action layer is not available in this process"}
        wanted = (status or "").strip()
        if wanted and wanted not in PROPOSAL_STATUSES:
            return {"error": f"unknown status {wanted!r}: must be one of "
                             f"{', '.join(PROPOSAL_STATUSES)}"}
        rows = await actions.list_proposals(
            status=wanted or None, datasource=(datasource or "").strip() or None,
            limit=_PROPOSAL_LIMIT_MAX,
        )
        visible = [p for p in rows if await _allowed(p)]
        return {
            "proposals": [_proposal_brief(p) for p in visible],
            "enabled": bool(getattr(actions, "enabled", False)),
        }

    @mcp.tool()
    async def fetch_proposal(proposal_id: str) -> dict[str, Any]:
        """Fetch one proposal with the payload a human approved, plus its
        approval trail and delivery receipts. Read-only — fetching is not a
        receipt; call ``ack_proposal`` once the action has actually been taken.
        """
        if actions is None:
            return {"error": "action layer is not available in this process"}
        pid = (proposal_id or "").strip()
        if not pid:
            return {"error": "proposal_id is required"}
        detail = await actions.get(pid)
        if detail is None:
            return await _missing(pid)
        p = detail["proposal"]
        if not await _allowed(p):
            return await _missing(pid)
        return {
            "proposal": _proposal_brief(p),
            # 被批准的就是这份 payload(提案创建时定稿,外送原样发)——
            # 拉通道取回同一份,不重新渲染。
            "payload": dict(p.payload or {}),
            "approvals": [
                {"user": a.user_id, "action": a.action, "comment": a.comment,
                 "at": a.created_at}
                for a in detail["approvals"]
            ],
            "deliveries": [
                {"channel": d.channel, "status": d.status,
                 "http_status": d.http_status, "error": d.error,
                 "at": d.attempted_at}
                for d in detail["deliveries"]
            ],
            "stale": bool(detail["stale"]),
        }

    @mcp.tool()
    async def ack_proposal(proposal_id: str, note: str = "") -> dict[str, Any]:
        """Receipt: the action has been carried out — marks the proposal
        ``delivered`` and records who acked it. Idempotent (a second ack on a
        delivered proposal is a no-op). This does NOT approve anything; only a
        human in the admin console can approve or reject.
        """
        if actions is None:
            return {"error": "action layer is not available in this process"}
        pid = (proposal_id or "").strip()
        if not pid:
            return {"error": "proposal_id is required"}
        detail = await actions.get(pid)
        if detail is None:
            return await _missing(pid)
        if not await _allowed(detail["proposal"]):
            return await _missing(pid)
        try:
            fresh = await actions.ack(pid, _actor(), note or "")
        except Exception as e:
            # ProposalError 的文案是给人看的(状态不对时说明当前状态与允许的动作)
            return {"error": str(e)}
        return {"proposal": _proposal_brief(fresh), "acked": True}

    return mcp
